"""統一檢索（T-20、T-22、T-65）：vault 範圍 → 各 kind 的 lexical／向量候選 → 一次 RRF
→ 預算裁切。

kinds：`note`、`chunk`（文件段落，T-65）；`concept` 已知但尚未實作。預設（None）＝
note + chunk。note 與 chunk 各跑 lexical＋vector，四路一次送進 `rrf_fuse`（設計 5.2）。

回傳契約（A4）：每項只有 `{id, kind, vault, title, summary, summary_source, score,
updated}`（note 另帶 `author`，A22），不含 body；全文走 `notes.get`。
chunk 另帶 `document_id`、`chunk_id`（＝`id`）、`locator`；title 為文件檔名、
summary 為該段摘錄（`summary_source: "excerpt"`），同樣受字數預算。

降級（D1 設計約束）：
- embedder 沒設定、拋例外、逾時、回空／維度不符向量 → 只走 lexical，
  `degraded=True` 並帶 `degraded_reason`（代碼）與 `degraded_detail`（訊息）
- 呼叫端明確要求 `mode="lexical"` 不算降級
- 部分 note 缺向量不算降級（RRF 下它們仍可由 lexical 命中），但以
  `missing_embeddings`（chunk 為 `missing_chunk_embeddings`）告知向量那一路涵蓋不完整
- 呼叫端可宣告某些 kind 在這個模式下不可用（`unavailable_kinds`，例如殼讀快照降級時
  快照不含文件）：它們列進 `unsupported_kinds`，不會安靜回空；只要求不可用的 kind 時
  回空結果（不拋例外）

服務不可達時讀本地快照屬 MCP 殼（D8），不在本層。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from lore_vault.notes.summary import LEAD_MAX_CHARS, clip, display_summary
from lore_vault.storage import chunk_vectors, fts, vectors
from lore_vault.storage.document_index import chunk_id, chunks_by_key, parse_chunk_id
from lore_vault.storage.documents import get_documents
from lore_vault.storage.notes import get_notes
from lore_vault.storage.vaults import resolve_read

from .embedder import REASON_UNAVAILABLE, Embedder, QueryVector, embed_text
from .rrf import RRF_K, rrf_fuse

KIND_NOTE = "note"
KIND_CHUNK = "chunk"
KIND_CONCEPT = "concept"
KNOWN_KINDS = frozenset({KIND_NOTE, KIND_CHUNK, KIND_CONCEPT})
SUPPORTED_KINDS = frozenset({KIND_NOTE, KIND_CHUNK})
# kinds=None 時查的種類（concept 是背景注入用途，不在預設內）
DEFAULT_KINDS = (KIND_NOTE, KIND_CHUNK)
SOURCE_EXCERPT = "excerpt"

MODE_HYBRID = "hybrid"
MODE_LEXICAL = "lexical"
MODE_VECTOR = "vector"
MODES = (MODE_HYBRID, MODE_LEXICAL, MODE_VECTOR)

LEG_LEXICAL = "lexical"
LEG_VECTOR = "vector"
# RRF 的四路（`legs` 對外只報方法：lexical／vector）
_RANKING_LEGS = {
    (KIND_NOTE, LEG_LEXICAL): "note_lexical",
    (KIND_NOTE, LEG_VECTOR): "note_vector",
    (KIND_CHUNK, LEG_LEXICAL): "chunk_lexical",
    (KIND_CHUNK, LEG_VECTOR): "chunk_vector",
}

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
    id: str  # note id，或 chunk id（`chunk:<uuid>:<idx>`）
    kind: str
    vault: str
    title: str  # note 標題；chunk 為文件檔名
    summary: str | None
    summary_source: str
    score: float
    updated: str
    # 以下僅 kind="chunk" 有值
    document_id: str | None = None
    chunk_id: str | None = None
    locator: dict[str, Any] | None = None
    # 僅 kind="note"：寫入者自報名（A22；未具名為 None）
    author: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "vault": self.vault,
            "title": self.title,
            "summary": self.summary,
            "summary_source": self.summary_source,
            "score": round(self.score, 6),
            "updated": self.updated,
        }
        if self.kind == KIND_NOTE:
            data["author"] = self.author
        if self.kind == KIND_CHUNK:
            data["document_id"] = self.document_id
            data["chunk_id"] = self.chunk_id
            data["locator"] = self.locator
        return data


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
    # 範圍內沒有可用向量的 note 數；None = 本次沒有跑向量那一路（或沒查 note）
    missing_embeddings: int | None = None
    legs: tuple[str, ...] = field(default_factory=tuple)
    # 實際查了哪些 kind
    kinds: tuple[str, ...] = ()
    # 範圍內可索引文件中沒有可用向量的 chunk 數；None = 沒跑 chunk 的向量那一路
    missing_chunk_embeddings: int | None = None

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
            "kinds": list(self.kinds),
            "missing_chunk_embeddings": self.missing_chunk_embeddings,
        }


def _check_kinds(
    kinds: Iterable[str] | None, unavailable: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """回傳 (要查的 kinds, 回報為不支援的 kinds)。

    未實作的 kind（concept）若是唯一要求的就拋 `UnsupportedKind`；
    此模式下不可用的 kind（`unavailable`）只回報、不拋。
    """
    if kinds is None:
        requested: tuple[str, ...] = DEFAULT_KINDS
    else:
        if isinstance(kinds, str):
            raise TypeError("kinds 必須是清單，不可傳單一字串")
        requested = tuple(dict.fromkeys(kinds))
        unknown = sorted(set(requested) - KNOWN_KINDS)
        if unknown:
            raise ValueError(f"未知的 kinds：{unknown}；可用 {sorted(KNOWN_KINDS)}")
        if not requested:
            raise ValueError("kinds 不可為空清單；用預設請傳 None")
    not_implemented = set(requested) - SUPPORTED_KINDS
    not_available = (set(requested) & set(unavailable)) - not_implemented
    if not_implemented and not_implemented == set(requested):
        raise UnsupportedKind(
            f"kinds {sorted(not_implemented)} 目前不支援檢索（concept 檢索尚未實作）"
        )
    skipped = not_implemented | not_available
    active = tuple(k for k in requested if k not in skipped)
    return active, tuple(sorted(skipped))


def chunk_excerpt(text: str, overlap: int = 0) -> str | None:
    """chunk 的摘錄：略過與前一段重疊的開頭，壓空白後截到 `LEAD_MAX_CHARS`。"""
    body = text[overlap:] if 0 < overlap < len(text) else text
    flat = " ".join(body.split()) or " ".join(text.split())
    return clip(flat, LEAD_MAX_CHARS)[0] if flat else None


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
        clipped = replace(item, title=title, summary=summary or None)
        return [clipped], True, len(items) - 1, len(title) + len(summary)
    return kept, False, 0, used


def recall(
    conn: sqlite3.Connection,
    query: str,
    vault: str,
    *,
    space: str,
    embedder: Embedder | None = None,
    dim: int | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    budget: int = DEFAULT_BUDGET,
    mode: str = MODE_HYBRID,
    rrf_k: int = RRF_K,
    unavailable_kinds: Iterable[str] = (),
) -> RecallResult:
    """在 `space` 內的 `vault`（或明示 `"*"`＝該 space 全部 vault）檢索 note 與文件段。

    `embedder` 與 `dim` 用於查詢向量；`mode="lexical"` 時不需要。
    `kinds` 預設 note + chunk；含 concept 時其他種類照回，並在 `unsupported_kinds`
    標示；只要求 concept 則拋 `UnsupportedKind`。`unavailable_kinds` 見模組說明。
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
    active, unsupported = _check_kinds(kinds, unavailable_kinds)
    # 先驗證 vault 與 space，範圍錯誤不應該先去打 embedder
    resolve_read(conn, vault, space=space)
    if not active:
        return RecallResult(
            items=[], mode=mode, budget=budget, unsupported_kinds=unsupported
        )

    depth = max(MIN_CANDIDATES, limit * CANDIDATE_FACTOR)
    rankings: dict[str, list[str]] = {}
    legs: list[str] = []
    degraded: QueryVector | None = None
    missing_embeddings: int | None = None
    missing_chunk_embeddings: int | None = None

    if mode in (MODE_HYBRID, MODE_VECTOR):
        if dim is None:
            qv = QueryVector(None, REASON_UNAVAILABLE, "未設定 embedding 維度")
        else:
            qv = embed_text(embedder, query, dim=dim)
        if qv.ok:
            assert dim is not None
            legs.append(LEG_VECTOR)
            if KIND_NOTE in active:
                hits = vectors.search_vectors(
                    conn, vault, qv.vector, space=space, dim=dim, limit=depth
                )
                rankings[_RANKING_LEGS[KIND_NOTE, LEG_VECTOR]] = [
                    h.note_id for h in hits
                ]
                missing_embeddings = vectors.count_without_vector(
                    conn, vault, space=space, dim=dim
                )
            if KIND_CHUNK in active:
                chunk_hits = chunk_vectors.search_chunk_vectors(
                    conn, vault, qv.vector, space=space, dim=dim, limit=depth
                )
                rankings[_RANKING_LEGS[KIND_CHUNK, LEG_VECTOR]] = [
                    chunk_id(h.document_id, h.idx) for h in chunk_hits
                ]
                missing_chunk_embeddings = chunk_vectors.count_chunks_without_vector(
                    conn, vault, space=space, dim=dim
                )
        else:
            degraded = qv
    if mode in (MODE_HYBRID, MODE_LEXICAL) or degraded is not None:
        legs.append(LEG_LEXICAL)
        if KIND_NOTE in active:
            hits = fts.search_notes(conn, vault, query, space=space, limit=depth)
            rankings[_RANKING_LEGS[KIND_NOTE, LEG_LEXICAL]] = [h.note_id for h in hits]
        if KIND_CHUNK in active:
            fts_hits = fts.search_chunks(conn, vault, query, space=space, limit=depth)
            rankings[_RANKING_LEGS[KIND_CHUNK, LEG_LEXICAL]] = [
                chunk_id(h.document_id, h.idx) for h in fts_hits
            ]

    fused = rrf_fuse(rankings, k=rrf_k)[:limit]
    items = _materialize(conn, vault, space, fused)
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
        legs=tuple(legs),
        kinds=active,
        missing_chunk_embeddings=missing_chunk_embeddings,
    )


def _materialize(
    conn: sqlite3.Connection, vault: str, space: str, fused: list[Any]
) -> list[RecallItem]:
    """融合後的 id → RecallItem（依排名）。候選查詢與讀取之間被刪除的略過。"""
    note_ids = [f.id for f in fused if parse_chunk_id(f.id) is None]
    chunk_keys = [key for f in fused if (key := parse_chunk_id(f.id)) is not None]
    notes = {n.id: n for n in get_notes(conn, vault, note_ids, space=space)}
    # 文件先在範圍內取（vault／space 硬過濾），chunk 只讀範圍內文件的
    documents = {
        d.id: d
        for d in get_documents(
            conn, vault, sorted({doc for doc, _ in chunk_keys}), space=space
        )
    }
    chunks = chunks_by_key(conn, [k for k in chunk_keys if k[0] in documents])
    items: list[RecallItem] = []
    for f in fused:
        key = parse_chunk_id(f.id)
        if key is None:
            note = notes.get(f.id)
            if note is None:
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
                    author=note.author,
                )
            )
            continue
        chunk = chunks.get(key)
        document = documents.get(key[0])
        if chunk is None or document is None:
            continue
        items.append(
            RecallItem(
                f.id,
                KIND_CHUNK,
                document.vault,
                document.filename,
                chunk_excerpt(chunk.text, chunk.overlap),
                SOURCE_EXCERPT,
                f.score,
                document.updated,
                document_id=document.id,
                chunk_id=f.id,
                locator=chunk.locator,
            )
        )
    return items
