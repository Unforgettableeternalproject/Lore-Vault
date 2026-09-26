"""Notes 服務層（T-21）：write（查重）、update（樂觀鎖）、get（預算）、list（分頁）。

「需要重算」的訊號落地在資料上，不靠記憶體內事件：
- body 變動 → 同一交易把 `summary` 清成 None。背景補算（enrich）以
  `summary IS NULL` 推導佇列，服務重啟也不會漏；清掉之前的舊摘要也不會被
  recall 當成最新摘要回傳（改以首段頂替、標 `summary_source: "lead"`）
- title／body 變動 → storage 同一交易刪除舊 embedding（缺向量即待補算）
- 只改 title／topics 不清 summary（D4）
回傳值另帶 `summary_stale`／`embedding_stale` 旗標，呼叫端可據以喚醒 worker。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lore_vault.documents import service as document_service
from lore_vault.recall.embedder import REASON_UNAVAILABLE, Embedder, embed_text
from lore_vault.schema import Note, validate_author
from lore_vault.schema.chars import check_fields
from lore_vault.storage import fts, vectors
from lore_vault.storage.db import transaction
from lore_vault.storage.notes import (
    get_note,
    get_notes,
    insert_note,
    list_notes,
    superseded_by,
    update_note_if,
)
from lore_vault.storage.timeutil import normalize_utc, utc_now
from lore_vault.storage.vaults import resolve_read, resolve_write

from .links import merge_links, resolve_body_links
from .summary import clip, display_summary
from .text import embedding_text

# ── 查重門檻 ────────────────────────────────────────────────────────
# 向量：bge-m3 cosine。2026-09-26 以合成句對實測（非正式語料）：改寫同義句
# 0.81／0.86／0.92（中／英／中英混合），同主題但不同事實 0.55，同領域無關 0.47，
# 無關 0.32–0.51。取兩群之間偏向重複側的 0.78。樣本少，待 1490 則匯入後以真實
# 語料校準。
DEDUP_VECTOR_THRESHOLD = 0.78
# lexical：title+body 的索引 token 集合 Jaccard（CJK bigram、英文詞，不分大小寫）。
# BM25 分數只在同一查詢內可比，不能當門檻；Jaccard 在 0–1 可解釋。
# 0.5 = 兩篇至少一半的詞彙相同，視為疑似重複；同樣待真實語料校準。
DEDUP_LEXICAL_THRESHOLD = 0.5
# 每一路取多少候選來計算相似度
DEDUP_CANDIDATES = 20
# 最多回幾筆疑似重複
DEDUP_MAX_RESULTS = 5
# lexical 候選查詢最多用幾個 token（避免長正文組出上千項的 MATCH）
DEDUP_QUERY_TOKENS = 64

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
# list 的摘要預算：本頁 note 摘要字數總和上限（title 一律回、不計入，
# 分頁不因預算少回）。
# 約 25 則首段頂替（≤160 字）；UI 可傳更大的值
DEFAULT_LIST_BUDGET = 4000
# 預算用完後的 note：summary 為 null、summary_source 標這個值（不沿用實際來源）
SOURCE_OMITTED = "omitted"
# get 的預算：所有回傳 body 的字數總和上限（約 3–4 篇中型 note）
DEFAULT_GET_BUDGET = 12000
MAX_GET_IDS = 50
# get 的 fields：full（預設，含全文）／meta（只回 metadata，不組裝全文）
FIELDS_FULL = "full"
FIELDS_META = "meta"
GET_FIELDS = (FIELDS_FULL, FIELDS_META)

_WHITESPACE = re.compile(r"\s+")


# ── 例外 ────────────────────────────────────────────────────────────


class VersionConflict(Exception):
    """`expected_updated` 與目前版本不符；不寫入。`current` 為目前版本。"""

    def __init__(self, current: Note, expected: str) -> None:
        self.current = current
        self.expected = expected
        super().__init__(
            f"note {current.id!r} 版本衝突：預期 {expected}，目前 {current.updated}"
        )


class NoChanges(ValueError):
    """update 沒有指定任何要改的欄位。"""


class InvalidCursor(ValueError):
    """list 的 cursor 無法解析。"""


# ── 共用 ────────────────────────────────────────────────────────────


def _token_set(text: str) -> set[str]:
    return {t.lower() for t in fts.tokens(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _norm_title(title: str) -> str:
    return _WHITESPACE.sub(" ", title).strip().casefold()


def _check_principal(principal: object) -> None:
    """principal 由服務依憑證決定；缺少代表呼叫路徑漏接認證結果，是程式錯誤。"""
    if not isinstance(principal, str) or not principal.strip():
        raise TypeError("principal 必填（由服務依憑證判定，不可由請求指定）")


def _str_list(name: str, values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} 必須是清單，不可傳單一字串")
    return tuple(values)


# ── write ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DuplicateCandidate:
    id: str
    title: str
    updated: str
    # "title"（標題相同）／"lexical"／"vector"
    reasons: tuple[str, ...]
    lexical: float
    vector: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "updated": self.updated,
            "reasons": list(self.reasons),
            "lexical": round(self.lexical, 4),
            "vector": None if self.vector is None else round(self.vector, 4),
        }


@dataclass(frozen=True)
class WriteResult:
    # dry_run 時為 None（沒有寫入）
    note: Note | None
    duplicates: list[DuplicateCandidate] = field(default_factory=list)
    # 查重的向量那一路沒跑（embedder 不可用等）：只做了 lexical 查重
    dedup_degraded: bool = False
    dedup_reason: str | None = None
    # 正文 `[[標題]]` 解析不到或歧義的連結（保留原文、不寫入 links）
    unresolved_links: tuple[dict[str, Any], ...] = ()
    dry_run: bool = False
    # 實際（或 dry_run 時將會）存下的 vault key 與 links
    vault: str = ""
    links: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        common = {
            "vault": self.vault,
            "links": list(self.links),
            "unresolved_links": [dict(u) for u in self.unresolved_links],
            "duplicates": [d.to_dict() for d in self.duplicates],
            "dedup_degraded": self.dedup_degraded,
            "dedup_reason": self.dedup_reason,
            "dry_run": self.dry_run,
        }
        if self.note is None:
            return common
        return {
            "id": self.note.id,
            "updated": self.note.updated,
            "author": self.note.author,
            "principal": self.note.principal,
            **common,
        }


def find_duplicates(
    conn: sqlite3.Connection,
    vault: str,
    title: str,
    body: str,
    *,
    space: str,
    embedder: Embedder | None = None,
    dim: int | None = None,
    vector_threshold: float = DEDUP_VECTOR_THRESHOLD,
    lexical_threshold: float = DEDUP_LEXICAL_THRESHOLD,
    exclude: Sequence[str] = (),
) -> tuple[list[DuplicateCandidate], str | None]:
    """同一 vault（與 space）內與 (title, body) 相似的 note。

    回傳 (疑似重複清單, 向量那一路的降級原因或 None)。
    """
    key = resolve_write(conn, vault, space=space)
    own_tokens = _token_set(f"{title} {body}")
    query_tokens = list(dict.fromkeys(fts.tokens(f"{title} {body}")))
    query = " ".join(query_tokens[:DEDUP_QUERY_TOKENS])
    candidate_ids: dict[str, None] = {}
    if query:
        for hit in fts.search_notes(
            conn, key, query, space=space, limit=DEDUP_CANDIDATES
        ):
            candidate_ids[hit.note_id] = None

    cosine: dict[str, float] = {}
    reason: str | None = None
    if dim is None:
        reason = REASON_UNAVAILABLE
    else:
        qv = embed_text(embedder, embedding_text(title, body), dim=dim)
        if qv.ok:
            for hit in vectors.search_vectors(
                conn, key, qv.vector, space=space, dim=dim, limit=DEDUP_CANDIDATES
            ):
                cosine[hit.note_id] = hit.score
                candidate_ids[hit.note_id] = None
        else:
            reason = qv.reason

    excluded = set(exclude)
    own_title = _norm_title(title)
    found: list[DuplicateCandidate] = []
    for note in get_notes(conn, key, list(candidate_ids), space=space):
        if note.id in excluded:
            continue
        lexical = _jaccard(own_tokens, _token_set(f"{note.title} {note.body}"))
        vec = cosine.get(note.id)
        reasons = []
        if _norm_title(note.title) == own_title:
            reasons.append("title")
        if lexical >= lexical_threshold:
            reasons.append("lexical")
        if vec is not None and vec >= vector_threshold:
            reasons.append("vector")
        if reasons:
            found.append(
                DuplicateCandidate(
                    note.id, note.title, note.updated, tuple(reasons), lexical, vec
                )
            )
    found.sort(key=lambda d: (-len(d.reasons), -max(d.lexical, d.vector or 0.0), d.id))
    return found[:DEDUP_MAX_RESULTS], reason


def write(
    conn: sqlite3.Connection,
    vault: str,
    title: str,
    body: str,
    *,
    space: str,
    principal: str,
    author: str | None = None,
    topics: Sequence[str] = (),
    links: Sequence[str] = (),
    supersedes: str | None = None,
    embedder: Embedder | None = None,
    dim: int | None = None,
    vector_threshold: float = DEDUP_VECTOR_THRESHOLD,
    lexical_threshold: float = DEDUP_LEXICAL_THRESHOLD,
    note_id: str | None = None,
    now: str | None = None,
    dry_run: bool = False,
) -> WriteResult:
    """新增 note，並回傳寫入前查到的疑似重複清單（由 agent 決定是否改用 update）。

    - 一律寫入；查重只提供資訊，是否改用 update 由 agent 決定
    - summary 與 embedding 不在這裡算，由背景補（A14）；查重會呼叫一次 embedder，
      失敗或未設定時只做 lexical 查重並標 `dedup_degraded`，不阻擋寫入
    - `supersedes` 必須是同一 vault 內存在的 note；它本身不列入疑似重複
    - 任一欄位含控制字元（tab、LF、CR 以外的 C0）或孤立 surrogate：拋
      `InvalidCharacters`，不寫入
    - 作者（A22）：`principal` 由呼叫端依憑證決定（HTTP 層取自認證結果，不可來自
      請求）；`author` 是寫入者自報名，未填存 None、不代填（規則見
      `schema.validate_author`）。建立時 `updated_by` 同 `author`
    - 連結：正文的 `[[標題]]` 在同一 vault 內依標題解析（`notes.links`），唯一命中的
      note id 併入 links（明確傳入的在前、去重）；解析不到或歧義的保留原文、不寫入，
      列在 `unresolved_links`
    - `dry_run=True`：驗證、範圍、supersedes 檢查、查重與連結解析都與正式寫入相同，
      只在寫入前停下（不建列、不動索引）
    """
    _check_principal(principal)
    author = validate_author(author)
    topics = _str_list("topics", topics)
    links = _str_list("links", links)
    # 查重會把 title／body 組成 FTS 查詢，禁用字元要在那之前擋下
    # （storage.insert_note 也會再擋一次，繞過服務層直接寫也進不去）
    check_fields(
        {
            "title": title,
            "body": body,
            "topics": topics,
            "links": links,
            "supersedes": supersedes,
        }
    )
    key = resolve_write(conn, vault, space=space)
    if supersedes is not None:
        get_note(conn, key, supersedes, space=space)  # 不存在拋 NotFound
    duplicates, reason = find_duplicates(
        conn,
        key,
        title,
        body,
        space=space,
        embedder=embedder,
        dim=dim,
        vector_threshold=vector_threshold,
        lexical_threshold=lexical_threshold,
        exclude=[supersedes] if supersedes else (),
    )
    parsed = resolve_body_links(conn, key, body, self_title=title)
    links = merge_links(links, parsed.ids)
    if dry_run:
        return WriteResult(
            None,
            duplicates,
            reason is not None,
            reason,
            parsed.unresolved,
            dry_run=True,
            vault=key,
            links=links,
        )
    stamp = normalize_utc(now) if now is not None else utc_now()
    note = Note(
        id=note_id or uuid.uuid4().hex,
        vault=key,
        title=title,
        body=body,
        created=stamp,
        updated=stamp,
        summary=None,
        topics=topics,
        links=links,
        supersedes=supersedes,
        author=author,
        principal=principal,
        updated_by=author,
        updated_by_principal=principal,
    )
    stored = insert_note(conn, key, note, space=space)
    return WriteResult(
        stored,
        duplicates,
        reason is not None,
        reason,
        parsed.unresolved,
        vault=stored.vault,
        links=stored.links,
    )


# ── update ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UpdateResult:
    note: Note
    # body 變了：summary 已清空，待背景重算
    summary_stale: bool
    # title 或 body 變了：舊 embedding 已刪，待背景重算
    embedding_stale: bool
    # 這次有重新解析正文連結時，解析不到或歧義的 `[[標題]]`
    unresolved_links: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.note.id,
            "vault": self.note.vault,
            "updated": self.note.updated,
            "author": self.note.author,
            "updated_by": self.note.updated_by,
            "updated_by_principal": self.note.updated_by_principal,
            "links": list(self.note.links),
            "unresolved_links": [dict(u) for u in self.unresolved_links],
            "summary_stale": self.summary_stale,
            "embedding_stale": self.embedding_stale,
        }


_UNSET: Any = object()


def update(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    expected_updated: str,
    *,
    space: str,
    principal: str,
    author: str | None = None,
    title: str | None = None,
    body: str | None = None,
    topics: Sequence[str] | None = None,
    links: Sequence[str] | None = None,
    supersedes: str | None = _UNSET,
    now: str | None = None,
) -> UpdateResult:
    """樂觀鎖更新：`expected_updated` 必須等於目前版本，否則拋 `VersionConflict`
    （附目前版本），不寫入任何東西。

    版本比對只在 `storage.notes.update_note_if` 一處（同一交易內）。
    `author` 是這次修改者的自報名：寫進 `updated_by`（未填存 None，不沿用上一位），
    `principal` 寫進 `updated_by_principal`；原作者 `author` 不變。

    連結（與 write 同一套 `[[標題]]` 解析，同一交易內讀目前版本計算）：
    - 有傳 `links`：links = 傳入值 ∪ 正文（新 body，未傳則目前 body）解析出的 id
    - 沒傳 `links`、body 有變：links = (目前 links − 舊 body 解析出的 id) ∪ 新 body
      解析出的 id。正文刪掉 `[[x]]` 後自動連結會消失，明確加的連結保留（但若它剛好
      也寫在舊 body 的 `[[ ]]` 裡，視為自動連結一併移除）
    - 兩者都沒有：links 不動、不重新解析（`unresolved_links` 為空）
    """
    _check_principal(principal)
    author = validate_author(author)
    if not isinstance(expected_updated, str) or not expected_updated:
        raise ValueError("expected_updated 必填（取自 get／write 回傳的 updated）")
    changes: dict[str, Any] = {}
    if title is not None:
        changes["title"] = title
    if body is not None:
        changes["body"] = body
    if topics is not None:
        changes["topics"] = _str_list("topics", topics)
    if links is not None:
        changes["links"] = _str_list("links", links)
    if supersedes is not _UNSET:
        changes["supersedes"] = supersedes
    if not changes:
        raise NoChanges("沒有指定要更新的欄位")
    check_fields(changes)

    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        current = get_note(conn, key, note_id, space=space)  # 不存在拋 NotFound
        if changes.get("supersedes") is not None:
            get_note(conn, key, changes["supersedes"], space=space)
        body_changed = "body" in changes and changes["body"] != current.body
        title_changed = "title" in changes and changes["title"] != current.title
        unresolved: tuple[dict[str, Any], ...] = ()
        if "links" in changes or body_changed:
            parsed = resolve_body_links(
                conn,
                key,
                changes.get("body", current.body),
                self_id=note_id,
                self_title=changes.get("title", current.title),
            )
            if "links" in changes:
                base = changes["links"]
            else:
                stale = resolve_body_links(
                    conn,
                    key,
                    current.body,
                    self_id=note_id,
                    self_title=current.title,
                ).ids
                base = tuple(i for i in current.links if i not in stale)
            changes["links"] = merge_links(base, parsed.ids)
            unresolved = parsed.unresolved
        if body_changed:
            changes["summary"] = None
        updated = update_note_if(
            conn,
            key,
            note_id,
            expected_updated,
            changes,
            space=space,
            now=now,
            editor=(author, principal),
        )
        if updated is None:
            raise VersionConflict(
                get_note(conn, key, note_id, space=space), expected_updated
            )
    return UpdateResult(
        updated, body_changed, body_changed or title_changed, unresolved
    )


# ── get ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GetResult:
    items: list[dict[str, Any]]
    # 範圍內找不到的 id（不存在或屬於其他 vault），依傳入順序
    missing: list[str]
    truncated: bool
    budget: int
    used_chars: int
    # 這個模式下查不了的 id（降級讀快照時的文件／chunk id）：不是不存在
    unavailable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "missing": self.missing,
            "unavailable": self.unavailable,
            "truncated": self.truncated,
            "budget": self.budget,
            "used_chars": self.used_chars,
        }


def get(
    conn: sqlite3.Connection,
    vault: str,
    ids: Sequence[str],
    *,
    space: str,
    budget: int = DEFAULT_GET_BUDGET,
    documents_available: bool = True,
    fields: str = FIELDS_FULL,
) -> GetResult:
    """批次取全文。`ids` 可混 note id、文件 id（`doc:…`，回整份文件文字）與
    chunk id（`chunk:…`，回該段全文），依前綴分派（T-66）。

    依傳入順序分配字數預算（note 的 body、文件／chunk 的 text）；超過時該項截斷、
    之後各項為空字串，並在該項標 `truncated: true`、`body_chars`／`text_chars`
    為原文字數。文件文字依 chunk 順序逐段取，預算用完就停（不先串全文）。
    chunk 項目帶 `overlap`（開頭與前一段重疊的字數，段落起頭為 0）。
    `fields="meta"`：只回 metadata——note 不含 `body`、文件／chunk 不含 `text`，
    `body_chars`／`text_chars` 照給、不佔預算（`used_chars` 為 0）、不組裝全文。
    `documents_available=False`（降級讀快照）時文件／chunk id 列在 `unavailable`。
    """
    if isinstance(ids, str):
        raise TypeError("ids 必須是清單，不可傳單一字串")
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise ValueError("ids 不可為空")
    if len(unique) > MAX_GET_IDS:
        raise ValueError(f"一次最多取 {MAX_GET_IDS} 則，得到 {len(unique)}")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"budget 必須是正整數，得到 {budget!r}")
    if fields not in GET_FIELDS:
        raise ValueError(f"fields 必須是 {list(GET_FIELDS)} 之一，得到 {fields!r}")
    meta_only = fields == FIELDS_META
    refs = [i for i in unique if document_service.is_document_ref(i)]
    note_ids = [i for i in unique if not document_service.is_document_ref(i)]
    notes = get_notes(conn, vault, note_ids, space=space)
    found = {n.id: n for n in notes}
    replaced = superseded_by(conn, notes)
    doc_items: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    if refs and documents_available:
        doc_items = document_service.resolve_refs(conn, vault, refs, space=space)
    elif refs:
        unavailable = refs
    items: list[dict[str, Any]] = []
    remaining = budget
    any_truncated = False
    for item_id in unique:
        doc_item = doc_items.get(item_id)
        if doc_item is not None:
            item = dict(doc_item)
            if doc_item["kind"] == document_service.KIND_CHUNK:
                full = item.pop("text")
                total = len(full)
                text = full[:remaining]
            elif meta_only:
                total = document_service.document_text_chars(conn, doc_item["id"])
                text = ""
            else:
                text, total = document_service.document_text(
                    conn, doc_item["id"], remaining
                )
            item["text_chars"] = total
            if meta_only:
                item["truncated"] = False
            else:
                truncated = len(text) < total
                remaining -= len(text)
                any_truncated = any_truncated or truncated
                item["text"] = text
                item["truncated"] = truncated
            items.append(item)
            continue
        note = found.get(item_id)
        if note is None:
            continue
        summary, source = display_summary(note)
        entry: dict[str, Any] = {
            "id": note.id,
            "kind": "note",
            "vault": note.vault,
            "title": note.title,
            "summary": summary,
            "summary_source": source,
        }
        if meta_only:
            entry["truncated"] = False
        else:
            body = note.body[:remaining]
            truncated = len(body) < len(note.body)
            remaining -= len(body)
            any_truncated = any_truncated or truncated
            entry["body"] = body
            entry["truncated"] = truncated
        entry["body_chars"] = len(note.body)
        items.append(
            {
                **entry,
                "topics": list(note.topics),
                "links": list(note.links),
                "supersedes": note.supersedes,
                "superseded_by": replaced.get(note.id),
                "author": note.author,
                "principal": note.principal,
                "updated_by": note.updated_by,
                "updated_by_principal": note.updated_by_principal,
                "created": note.created,
                "updated": note.updated,
            }
        )
    missing = [
        i
        for i in unique
        if i not in found and i not in doc_items and i not in unavailable
    ]
    return GetResult(
        items, missing, any_truncated, budget, budget - remaining, unavailable
    )


# ── list ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ListResult:
    items: list[dict[str, Any]]
    next_cursor: str | None
    # 要求了但這個模式下列不出來的種類（降級讀快照時的 document）
    unsupported_kinds: list[str] = field(default_factory=list)
    # 摘要預算：本頁 note 摘要字數總和上限與實際用量
    budget: int = DEFAULT_LIST_BUDGET
    used_chars: int = 0
    # 預算用完、summary 被省略（`summary_source: "omitted"`）的 note 數；
    # truncated=True 且 omitted 為 0 代表只截了一筆的摘要
    summaries_omitted: int = 0
    truncated: bool = False

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "unsupported_kinds": self.unsupported_kinds,
            "budget": self.budget,
            "used_chars": self.used_chars,
            "truncated": self.truncated,
            "summaries_omitted": self.summaries_omitted,
        }


def _encode_cursor(cursor: tuple[str, str]) -> str:
    raw = json.dumps(list(cursor), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidCursor(f"cursor 無法解析：{cursor!r}") from exc
    if (
        not isinstance(data, list)
        or len(data) != 2
        or not all(isinstance(x, str) for x in data)
    ):
        raise InvalidCursor(f"cursor 格式錯誤：{cursor!r}")
    return data[0], data[1]


LIST_KIND_NOTE = "note"
LIST_KIND_DOCUMENT = "document"
LIST_KINDS = (LIST_KIND_NOTE, LIST_KIND_DOCUMENT)


def _list_kinds(kinds: Sequence[str] | None) -> tuple[str, ...]:
    if kinds is None:
        return LIST_KINDS
    if isinstance(kinds, str):
        raise TypeError("kinds 必須是清單，不可傳單一字串")
    requested = tuple(dict.fromkeys(kinds))
    if not requested:
        raise ValueError("kinds 不可為空清單；用預設請傳 None")
    unknown = sorted(set(requested) - set(LIST_KINDS))
    if unknown:
        raise ValueError(f"未知的 kinds：{unknown}；可用 {list(LIST_KINDS)}")
    return requested


def list_(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    since: str | None = None,
    topics: Sequence[str] | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    kinds: Sequence[str] | None = None,
    documents_available: bool = True,
    budget: int = DEFAULT_LIST_BUDGET,
) -> ListResult:
    """標題清單，依 updated 由新到舊；note 與文件（`kinds` 預設兩者）合併分頁。

    `next_cursor` 非 None 代表還有下一頁。文件沒有 topics：指定 `topics` 時只列 note。
    `documents_available=False`（降級讀快照）時 document 列在 `unsupported_kinds`。

    note 項目帶 `summary`／`summary_source`（規則同 recall：有 LLM 摘要用它，否則正文
    首段頂替）與 `superseded_by`（同 vault 內取代它的 note，多則取 updated 最新者）。
    摘要受 `budget` 限制（本頁 note 摘要字數總和；title 不計、項目一律回，分頁不受
    影響）：依本頁順序放入，第一則就超過時截斷該則，之後預算不足的 note
    `summary: null`、`summary_source: "omitted"`。
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit 必須是整數")
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit 必須在 1–{MAX_LIST_LIMIT}，得到 {limit}")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"budget 必須是正整數，得到 {budget!r}")
    wanted = _list_kinds(kinds)
    decoded = _decode_cursor(cursor) if cursor is not None else None
    unsupported: list[str] = []
    rows: list[tuple[str, str, dict[str, Any]]] = []
    more = False
    if LIST_KIND_NOTE in wanted:
        notes, next_notes = list_notes(
            conn,
            vault,
            space=space,
            since=since,
            topics=topics,
            limit=limit,
            cursor=decoded,
        )
        more = more or next_notes is not None
        replaced = superseded_by(conn, notes)
        rows.extend(
            (
                n.updated,
                n.id,
                {
                    "id": n.id,
                    "kind": LIST_KIND_NOTE,
                    "vault": n.vault,
                    "title": n.title,
                    "topics": list(n.topics),
                    "author": n.author,
                    "updated_by": n.updated_by,
                    "supersedes": n.supersedes,
                    "superseded_by": replaced.get(n.id),
                    "updated": n.updated,
                    # 暫存：預算分配後換成 summary／summary_source
                    "_note": n,
                },
            )
            for n in notes
        )
    if LIST_KIND_DOCUMENT in wanted and topics is None:
        if documents_available:
            page, next_docs = document_service.list_page(
                conn,
                vault,
                space=space,
                since=normalize_utc(since) if since is not None else None,
                limit=limit,
                cursor=decoded,
            )
            more = more or next_docs is not None
            rows.extend(page)
        else:
            resolve_read(conn, vault, space=space)
            unsupported.append(LIST_KIND_DOCUMENT)
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    more = more or len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        _encode_cursor((page_rows[-1][0], page_rows[-1][1]))
        if more and page_rows
        else None
    )
    items = [r[2] for r in page_rows]
    used, omitted, truncated = _apply_list_budget(items, budget)
    return ListResult(
        items,
        next_cursor,
        unsupported,
        budget=budget,
        used_chars=used,
        summaries_omitted=omitted,
        truncated=truncated,
    )


def _apply_list_budget(
    items: list[dict[str, Any]], budget: int
) -> tuple[int, int, bool]:
    """依本頁順序替 note 項目填摘要（就地改寫）。回傳 (已用字數, 省略筆數, 是否截斷)。

    比照 recall 的預算：第一則就超過時截斷它的摘要（至少給一則），之後放不下的一律
    省略、不在中間項目截摘要（避免看起來像完整摘要）。
    """
    used = omitted = 0
    exhausted = truncated = False
    for item in items:
        note = item.pop("_note", None)
        if note is None:
            continue
        summary, source = display_summary(note)
        cost = len(summary or "")
        if not exhausted and used + cost <= budget:
            used += cost
        elif not exhausted and used == 0:
            summary, _ = clip(summary or "", budget)
            used = len(summary)
            exhausted = truncated = True
        else:
            exhausted = truncated = True
            summary, source = None, SOURCE_OMITTED
            omitted += 1
        item["summary"] = summary
        item["summary_source"] = source
    return used, omitted, truncated
