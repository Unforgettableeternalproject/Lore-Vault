"""`ask()`（D11）：recall 取回的 note 片段交問答模型整理成逐點回答。

流程分兩段，讓呼叫端在呼叫模型前就釋放 SQLite 連線：

1. `prepare(conn, ...)`：呼叫與 `/v1/recall` 同一個 `recall()`（不另寫檢索）取前 k 則
   note，再以同一個 vault 範圍取全文，組成片段（標題、LLM 摘要（缺摘要時不放，
   首段頂替與正文重複）、正文節錄、updated、supersedes；正文節錄上限
   `ask.snippet_max_chars`）
2. `generate(context, answerer)`：組 prompt → 模型結構化輸出
   `{status, points[{claim, note_ids}]}` → 機械防呆

範圍（本輪）：**只針對 note**。`kinds` 預設 `["note"]`；要求 `chunk`（文件段落）時
列進 `unsupported_kinds`（文件問答另評估），只要求 chunk 則拋 `UnsupportedKind`。

機械防呆：
- 引用的 note id 不在本次片段清單內 → 從該點移除，記在 `dropped_citations`
- 某點沒有任何有效引用（全被移除或本來就空）→ 該點保留並標 `unsupported: true`
  （不刪除：讓呼叫端看得到模型說了什麼，但知道它沒有依據）
- `status="answered"` 但沒有任何一點有有效引用 → 改為 `insufficient`，
  並標 `status_downgraded: true`
- 模型輸出空字串、被截斷、不是 JSON、結構不符 → `AskInvalidOutput`（不回假成功）

沒有任何片段時不呼叫模型，直接回 `insufficient`（`model`、`usage` 為 null）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from lore_vault.enrich.clients import (
    EnrichError,
    EnrichTimeout,
    InvalidOutput,
    ProviderUnavailable,
    RateLimited,
)
from lore_vault.notes.summary import SOURCE_SUMMARY, clip, display_summary
from lore_vault.recall import UnsupportedKind
from lore_vault.recall import recall as recall_service
from lore_vault.recall.embedder import Embedder
from lore_vault.recall.service import KIND_CHUNK, KIND_NOTE, MODE_HYBRID
from lore_vault.storage.notes import get_notes

from .client import STATUS_ANSWERED, STATUS_INSUFFICIENT, STATUSES, Answerer

DEFAULT_K = 10
# k 上限：成本隨 k × snippet_max_chars 成長，不沿用 recall 的 100
MAX_K = 20
DEFAULT_SNIPPET_MAX_CHARS = 6000
DEFAULT_KINDS = (KIND_NOTE,)
# 交給 recall 的字數預算：ask 自己取全文，recall 的 title+summary 預算只會讓
# 結果少於 k，所以給一個到不了的上限
_RECALL_BUDGET = 1_000_000

NOTICE = (
    "回答是檢索片段的整理，信心有限；關鍵事實請以 get 核對原 note。"
    "本版只使用 note，文件段落的問答另行評估。"
)

SYSTEM_PROMPT = (
    "你是 Lore Vault 的問答助手。你只能根據下方提供的『筆記片段』回答問題，"
    "不可使用片段以外的知識、不可推測或補充片段沒有的資訊。\n"
    "規則：\n"
    "1. 逐點列出你的主張（points），每一點都要標出支持它的 note id（note_ids），"
    "只能使用片段標頭中出現的 note_id。\n"
    "2. 如果片段不足以完整或部分回答問題，status 設為 insufficient，"
    "並在 points 中盡量列出片段裡有、但不足以完整回答的部分；若完全沒有相關內容，"
    "points 給空陣列。\n"
    "3. 不要對檢索結果給予過高信心；如果片段之間有矛盾或新舊不一致，"
    "在 points 中明確指出（用一點描述矛盾，note_ids 帶兩邊）。\n"
    "4. 只根據片段的 updated 時間與 supersedes（取代關係）判斷新舊，不要臆測。\n"
    "5. 用繁體中文回答。只輸出 JSON，符合給定 schema，不要多餘文字。"
)


class AskError(Exception):
    """ask 生成階段失敗；`code` 為對外錯誤碼。"""

    code = "ask_failed"
    http_status = 500


class AskNotConfigured(AskError):
    """服務沒有 OpenAI key（或未建立問答用戶端）。"""

    code = "ask_not_configured"


class AskProviderError(AskError):
    """模型服務連不上、認證或模型設定錯、5xx。"""

    code = "ask_provider_error"


class AskTimeout(AskError):
    code = "ask_timeout"


class AskRateLimited(AskError):
    code = "ask_rate_limited"
    http_status = 429

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AskInvalidOutput(AskError):
    """空輸出、截斷、不是 JSON、結構不符。"""

    code = "ask_invalid_output"


@dataclass(frozen=True)
class Snippet:
    id: str
    vault: str
    title: str
    summary: str | None
    summary_source: str
    body: str
    body_truncated: bool
    updated: str
    supersedes: str | None
    score: float

    def source_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "vault": self.vault,
            "title": self.title,
            "updated": self.updated,
            "score": round(self.score, 6),
            "excerpt_truncated": self.body_truncated,
        }


@dataclass(frozen=True)
class AskContext:
    """`prepare` 的結果：片段與檢索中繼資料（不含連線）。"""

    question: str
    k: int
    snippets: list[Snippet]
    kinds: tuple[str, ...]
    unsupported_kinds: tuple[str, ...]
    degraded: bool
    degraded_reason: str | None
    degraded_detail: str | None
    missing_embeddings: int | None
    retrieval_ms: int


@dataclass(frozen=True)
class AnswerPoint:
    claim: str
    note_ids: tuple[str, ...]
    unsupported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "note_ids": list(self.note_ids),
            "unsupported": self.unsupported,
        }


@dataclass(frozen=True)
class AskResult:
    status: str
    points: list[AnswerPoint]
    context: AskContext
    dropped_citations: list[dict[str, Any]] = field(default_factory=list)
    status_downgraded: bool = False
    model: str | None = None
    usage: dict[str, int] | None = None
    generation_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        ctx = self.context
        return {
            "status": self.status,
            "answer": {"points": [p.to_dict() for p in self.points]},
            "dropped_citations": list(self.dropped_citations),
            "status_downgraded": self.status_downgraded,
            "sources": [s.source_dict() for s in ctx.snippets],
            "k": ctx.k,
            "kinds": list(ctx.kinds),
            "unsupported_kinds": list(ctx.unsupported_kinds),
            "degraded": ctx.degraded,
            "degraded_reason": ctx.degraded_reason,
            "degraded_detail": ctx.degraded_detail,
            "missing_embeddings": ctx.missing_embeddings,
            "model": self.model,
            "usage": self.usage,
            "latency_ms": {
                "retrieval": ctx.retrieval_ms,
                "generation": self.generation_ms,
                "total": ctx.retrieval_ms + (self.generation_ms or 0),
            },
            "notice": NOTICE,
        }


def _ms(start: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - start) * 1000))


def _check_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k 必須是整數")
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k 必須在 1–{MAX_K}，得到 {k}")


def prepare(
    conn: sqlite3.Connection,
    question: str,
    vault: str,
    *,
    space: str,
    embedder: Embedder | None,
    dim: int | None,
    k: int = DEFAULT_K,
    kinds: Iterable[str] | None = None,
    snippet_max_chars: int = DEFAULT_SNIPPET_MAX_CHARS,
    clock: Callable[[], float] = time.monotonic,
) -> AskContext:
    """檢索並組成片段。參數驗證、vault／space 範圍與降級都沿用 `recall()`。"""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 不可為空")
    _check_k(k)
    if snippet_max_chars <= 0:
        raise ValueError("snippet_max_chars 必須大於 0")
    requested = DEFAULT_KINDS if kinds is None else kinds
    start = clock()
    result = recall_service(
        conn,
        question,
        vault,
        space=space,
        embedder=embedder,
        dim=dim,
        kinds=requested,
        limit=k,
        budget=_RECALL_BUDGET,
        mode=MODE_HYBRID,
        # 本輪只做 note：文件段落列進 unsupported_kinds，不送進模型
        unavailable_kinds=(KIND_CHUNK,),
    )
    if not result.kinds:
        raise UnsupportedKind(
            "ask 目前只支援 kinds=['note']；文件段落（chunk）的問答另行評估"
        )
    ids = [item.id for item in result.items if item.kind == KIND_NOTE]
    scores = {item.id: item.score for item in result.items}
    snippets: list[Snippet] = []
    # 同一個範圍取全文；recall 與讀取之間被刪除的 note 自然略過
    for note in get_notes(conn, vault, ids, space=space):
        summary, source = display_summary(note)
        body, truncated = clip(note.body, snippet_max_chars)
        snippets.append(
            Snippet(
                id=note.id,
                vault=note.vault,
                title=note.title,
                summary=summary,
                summary_source=source,
                body=body,
                body_truncated=truncated,
                updated=note.updated,
                supersedes=note.supersedes,
                score=scores[note.id],
            )
        )
    return AskContext(
        question=question,
        k=k,
        snippets=snippets,
        kinds=result.kinds,
        unsupported_kinds=result.unsupported_kinds,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        degraded_detail=result.degraded_detail,
        missing_embeddings=result.missing_embeddings,
        retrieval_ms=_ms(start, clock),
    )


def build_user_prompt(question: str, snippets: list[Snippet]) -> str:
    parts = [f"問題：{question}", "筆記片段："]
    for s in snippets:
        header = f"--- note_id={s.id} vault={s.vault} updated={s.updated}"
        if s.supersedes:
            header += f" supersedes={s.supersedes}"
        lines = [f"{header} ---", f"標題：{s.title}"]
        # 首段頂替（lead）與正文開頭重複，只放 LLM 摘要
        if s.summary and s.summary_source == SOURCE_SUMMARY:
            lines.append(f"摘要：{s.summary}")
        suffix = "（正文過長，已截斷）" if s.body_truncated else ""
        lines.append(f"正文{suffix}：\n{s.body}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _parse(content: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """模型輸出 → (status, [(claim, note_ids)])；任何結構不符都拋 AskInvalidOutput。"""
    try:
        data = json.loads(content)
    except ValueError:
        raise AskInvalidOutput("模型輸出不是合法 JSON") from None
    if not isinstance(data, dict):
        raise AskInvalidOutput("模型輸出不是 JSON 物件")
    status = data.get("status")
    if status not in STATUSES:
        raise AskInvalidOutput(f"模型輸出的 status 不合法：{status!r}")
    raw_points = data.get("points")
    if not isinstance(raw_points, list):
        raise AskInvalidOutput("模型輸出缺少 points 陣列")
    points: list[tuple[str, list[str]]] = []
    for index, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise AskInvalidOutput(f"points[{index}] 不是物件")
        claim = raw.get("claim")
        note_ids = raw.get("note_ids")
        if not isinstance(claim, str) or not claim.strip():
            raise AskInvalidOutput(f"points[{index}].claim 為空或不是字串")
        if not isinstance(note_ids, list) or not all(
            isinstance(i, str) for i in note_ids
        ):
            raise AskInvalidOutput(f"points[{index}].note_ids 不是字串陣列")
        points.append((claim.strip(), note_ids))
    return status, points


def guard_citations(
    status: str, raw_points: list[tuple[str, list[str]]], allowed: Iterable[str]
) -> tuple[str, list[AnswerPoint], list[dict[str, Any]], bool]:
    """引用防呆。回傳 (status, points, dropped_citations, status_downgraded)。"""
    allowed_ids = set(allowed)
    points: list[AnswerPoint] = []
    dropped: list[dict[str, Any]] = []
    for index, (claim, note_ids) in enumerate(raw_points):
        kept: list[str] = []
        for note_id in dict.fromkeys(i.strip() for i in note_ids):
            if note_id in allowed_ids:
                kept.append(note_id)
            else:
                dropped.append({"point": index, "note_id": note_id})
        points.append(AnswerPoint(claim, tuple(kept), unsupported=not kept))
    downgraded = False
    if status == STATUS_ANSWERED and not any(not p.unsupported for p in points):
        status = STATUS_INSUFFICIENT
        downgraded = True
    return status, points, dropped, downgraded


def generate(
    context: AskContext,
    answerer: Answerer | None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> AskResult:
    """呼叫模型並套用防呆。沒有片段時不呼叫模型。"""
    if not context.snippets:
        return AskResult(status=STATUS_INSUFFICIENT, points=[], context=context)
    if answerer is None:
        raise AskNotConfigured(
            "服務未設定問答模型（缺少 OPENAI_API_KEY），ask 無法使用"
        )
    prompt = build_user_prompt(context.question, context.snippets)
    start = clock()
    try:
        completion = answerer.complete(SYSTEM_PROMPT, prompt)
    except RateLimited as exc:
        raise AskRateLimited(str(exc), exc.retry_after) from None
    except EnrichTimeout as exc:
        raise AskTimeout(str(exc)) from None
    except InvalidOutput as exc:
        raise AskInvalidOutput(str(exc)) from None
    except (ProviderUnavailable, EnrichError, OSError) as exc:
        raise AskProviderError(str(exc)) from None
    elapsed = _ms(start, clock)
    status, raw_points = _parse(completion.content)
    status, points, dropped, downgraded = guard_citations(
        status, raw_points, (s.id for s in context.snippets)
    )
    return AskResult(
        status=status,
        points=points,
        context=context,
        dropped_citations=dropped,
        status_downgraded=downgraded,
        model=completion.model,
        usage=dict(completion.usage),
        generation_ms=elapsed,
    )


def ask(
    conn: sqlite3.Connection,
    question: str,
    vault: str,
    *,
    space: str,
    answerer: Answerer | None,
    embedder: Embedder | None,
    dim: int | None,
    k: int = DEFAULT_K,
    kinds: Iterable[str] | None = None,
    snippet_max_chars: int = DEFAULT_SNIPPET_MAX_CHARS,
) -> AskResult:
    """`prepare` + `generate`（同一條連線；HTTP 路由會在兩段之間釋放連線）。"""
    context = prepare(
        conn,
        question,
        vault,
        space=space,
        embedder=embedder,
        dim=dim,
        k=k,
        kinds=kinds,
        snippet_max_chars=snippet_max_chars,
    )
    return generate(context, answerer)
