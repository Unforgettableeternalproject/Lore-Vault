"""統一檢索（T-20、T-22 服務端）：vault 範圍 → lexical／向量兩路候選 → RRF → 預算裁切。

回傳契約（A4）：每項只有 `{id, kind, vault, title, summary, summary_source, score,
updated}`，不含 body；全文走 `notes.get`。

降級（D1 設計約束）：
- embedder 沒設定、拋例外、逾時、回空／維度不符向量 → 只走 lexical，
  `degraded=True` 並帶 `degraded_reason`（代碼）與 `degraded_detail`（訊息）
- 呼叫端明確要求 `mode="lexical"` 不算降級
- 部分 note 缺向量不算降級（RRF 下它們仍可由 lexical 命中），但以
  `missing_embeddings` 告知向量那一路涵蓋不完整

服務不可達時讀本地快照屬 MCP 殼（D8），不在本層。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from lore_vault.notes.summary import clip, display_summary
from lore_vault.storage import fts, vectors
from lore_vault.storage.notes import get_notes
from lore_vault.storage.vaults import resolve_read

from .embedder import REASON_UNAVAILABLE, Embedder, QueryVector, embed_text
from .rrf import RRF_K, rrf_fuse

KIND_NOTE = "note"
KIND_CONCEPT = "concept"
KNOWN_KINDS = frozenset({KIND_NOTE, KIND_CONCEPT})
SUPPORTED_KINDS = frozenset({KIND_NOTE})

MODE_HYBRID = "hybrid"
MODE_LEXICAL = "lexical"
MODE_VECTOR = "vector"
MODES = (MODE_HYBRID, MODE_LEXICAL, MODE_VECTOR)

LEG_LEXICAL = "lexical"
LEG_VECTOR = "vector"

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
# 字數預算：所有結果的 title + summary 字數總和上限。
# 10 筆 ×（標題 ~30 字 + 摘要 1–2 句 ~80 字）≈ 1100 字，留一倍餘裕給首段頂替（≤160 字）
DEFAULT_BUDGET = 2000
# 每一路取多少候選進 RRF：至少 50，且不少於 limit 的 5 倍，
# 讓只在其中一路排前面的 note 也有機會進入最終前 limit 名
MIN_CANDIDATES = 50
CANDIDATE_FACTOR = 5


class UnsupportedKind(ValueError):
    """要求的 kinds 目前沒有任何一種可檢索（concept 檢索尚未實作）。"""


@dataclass(frozen=True)
class RecallItem:
    id: str
    kind: str
    vault: str
    title: str
    summary: str | None
    summary_source: str
    score: float
    updated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "vault": self.vault,
            "title": self.title,
            "summary": self.summary,
            "summary_source": self.summary_source,
            "score": round(self.score, 6),
            "updated": self.updated,
        }


@dataclass(frozen=True)
class RecallResult:
    items: list[RecallItem]
    mode: str
    degraded: bool = False
    degraded_reason: str | None = None
    degraded_detail: str | None = None
    # 預算裁切：truncated=True 時 omitted 為被丟掉的結果數（0 代表只截了第一筆的摘要）
    truncated: bool = False
    omitted: int = 0
    budget: int = DEFAULT_BUDGET
    used_chars: int = 0
    # 要求了但目前不支援的 kinds（例如 concept）；明確回報，不默默忽略
    unsupported_kinds: tuple[str, ...] = ()
    # 範圍內沒有可用向量的 note 數；None = 本次沒有跑向量那一路
    missing_embeddings: int | None = None
    legs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "mode": self.mode,
            "legs": list(self.legs),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "degraded_detail": self.degraded_detail,
            "truncated": self.truncated,
            "omitted": self.omitted,
            "budget": self.budget,
            "used_chars": self.used_chars,
            "unsupported_kinds": list(self.unsupported_kinds),
            "missing_embeddings": self.missing_embeddings,
        }


def _check_kinds(kinds: Iterable[str] | None) -> tuple[str, ...]:
    if kinds is None:
        return ()
    if isinstance(kinds, str):
        raise TypeError("kinds 必須是清單，不可傳單一字串")
    kinds = tuple(kinds)
    unknown = sorted(set(kinds) - KNOWN_KINDS)
    if unknown:
        raise ValueError(f"未知的 kinds：{unknown}；可用 {sorted(KNOWN_KINDS)}")
    if not kinds:
        raise ValueError("kinds 不可為空清單；用預設請傳 None")
    unsupported = tuple(sorted(set(kinds) - SUPPORTED_KINDS))
    if len(unsupported) == len(set(kinds)):
        raise UnsupportedKind(
            f"kinds {list(unsupported)} 目前不支援檢索（concept 檢索尚未實作）"
        )
    return unsupported


def _apply_budget(
    items: list[RecallItem], budget: int
) -> tuple[list[RecallItem], bool, int, int]:
    """依排名放入結果直到字數預算用完。回傳 (結果, 是否截斷, 丟掉幾筆, 已用字數)。

    第一筆本身就超過預算時截它的標題／摘要（至少回一筆），其餘整筆丟掉——
    不在中間項目截摘要，避免看起來像完整摘要。
    """
    kept: list[RecallItem] = []
    used = 0
    for index, item in enumerate(items):
        cost = len(item.title) + len(item.summary or "")
        if used + cost <= budget:
            kept.append(item)
            used += cost
            continue
        if kept:
            return kept, True, len(items) - index, used
        title, _ = clip(item.title, budget)
        summary, _ = clip(item.summary or "", budget - len(title))
        clipped = RecallItem(
            item.id,
            item.kind,
            item.vault,
            title,
            summary or None,
            item.summary_source,
            item.score,
            item.updated,
        )
        return [clipped], True, len(items) - 1, len(title) + len(summary)
    return kept, False, 0, used


def recall(
    conn: sqlite3.Connection,
    query: str,
    vault: str,
    *,
    embedder: Embedder | None = None,
    dim: int | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    budget: int = DEFAULT_BUDGET,
    mode: str = MODE_HYBRID,
    rrf_k: int = RRF_K,
) -> RecallResult:
    """在 `vault`（或明示的 `"*"`）內檢索 note。

    `embedder` 與 `dim` 用於查詢向量；`mode="lexical"` 時不需要。
    `kinds` 預設只有 note；含 concept 時結果仍回 note，並在 `unsupported_kinds`
    標示；只要求 concept 則拋 `UnsupportedKind`。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不可為空")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit 必須是整數")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必須在 1–{MAX_LIMIT}，得到 {limit}")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"budget 必須是正整數，得到 {budget!r}")
    if mode not in MODES:
        raise ValueError(f"mode 必須是 {list(MODES)}，得到 {mode!r}")
    unsupported = _check_kinds(kinds)
    # 先驗證 vault，範圍錯誤不應該先去打 embedder
    resolve_read(conn, vault)

    depth = max(MIN_CANDIDATES, limit * CANDIDATE_FACTOR)
    rankings: dict[str, list[str]] = {}
    degraded: QueryVector | None = None
    missing_embeddings: int | None = None

    if mode in (MODE_HYBRID, MODE_VECTOR):
        if dim is None:
            qv = QueryVector(None, REASON_UNAVAILABLE, "未設定 embedding 維度")
        else:
            qv = embed_text(embedder, query, dim=dim)
        if qv.ok:
            assert dim is not None
            hits = vectors.search_vectors(conn, vault, qv.vector, dim=dim, limit=depth)
            rankings[LEG_VECTOR] = [h.note_id for h in hits]
            missing_embeddings = vectors.count_without_vector(conn, vault, dim=dim)
        else:
            degraded = qv
    if mode in (MODE_HYBRID, MODE_LEXICAL) or degraded is not None:
        hits = fts.search_notes(conn, vault, query, limit=depth)
        rankings[LEG_LEXICAL] = [h.note_id for h in hits]

    fused = rrf_fuse(rankings, k=rrf_k)[:limit]
    notes = {n.id: n for n in get_notes(conn, vault, [f.id for f in fused])}
    items: list[RecallItem] = []
    for f in fused:
        note = notes.get(f.id)
        if note is None:  # 候選查詢與取 note 之間被刪除
            continue
        summary, source = display_summary(note)
        items.append(
            RecallItem(
                note.id,
                KIND_NOTE,
                note.vault,
                note.title,
                summary,
                source,
                f.score,
                note.updated,
            )
        )
    kept, truncated, omitted, used = _apply_budget(items, budget)
    return RecallResult(
        items=kept,
        mode=mode,
        degraded=degraded is not None,
        degraded_reason=degraded.reason if degraded else None,
        degraded_detail=degraded.detail if degraded else None,
        truncated=truncated,
        omitted=omitted,
        budget=budget,
        used_chars=used,
        unsupported_kinds=unsupported,
        missing_embeddings=missing_embeddings,
        legs=tuple(rankings),
    )
