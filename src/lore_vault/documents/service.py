"""文件服務層（T-66／T-67）：上傳（去重、重試、新版本）與 get／list 用的文件項目。

上傳語意（B1 裁決，同 vault 內判斷；vault／space 硬範圍同 note）：

- 同 sha256 的既有文件為 ready／pending／extracting 且未被取代：回既有文件，
  `duplicate: true`，不建新列、不重新排隊。
- 同 sha256 的現行文件只有 failed：沿用最新那一列改回 pending 重跑抽取
  （檔名／MIME 換成這次的），`retried: true`。
- 同 sha256 的既有文件都已被新版本取代（使用者改回舊內容）：當成新內容，走下兩條。
- 同檔名、內容不同：新版本。`supersedes` 指向同檔名的現行版本（非 failed、未被取代），
  `version` = 該檔名最大版本 + 1；舊版在新版 ready 時退出索引，但仍可 get／list
  （標 `superseded_by`）。
- 其他：新文件（version 1）。

blob 先寫（冪等、交易外），判斷與建列在同一個 `BEGIN IMMEDIATE` 交易內（並行上傳同內容
只會有一列）。大小上限與格式判定在寫 blob 前就擋（不佔位、不進佇列）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from lore_vault.schema.chars import check_fields
from lore_vault.storage import documents as store
from lore_vault.storage.blobs import BlobStore, sha256_bytes
from lore_vault.storage.db import transaction
from lore_vault.storage.document_index import (
    chunk_id,
    chunks_by_key,
    chunks_of,
    parse_chunk_id,
)
from lore_vault.storage.documents import Document
from lore_vault.storage.vaults import resolve_write

from .chunking import join_chunks
from .extract import TOO_LARGE, ExtractionError, detect_format

DEFAULT_MIME = "application/octet-stream"
KIND_DOCUMENT = "document"
KIND_CHUNK = "chunk"


class UploadRejected(ValueError):
    """上傳在建列前就被拒絕（過大、格式不支援）。`code` 對應抽取錯誤碼。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def clean_filename(raw: str) -> str:
    """只留檔名本身（去掉客戶端路徑）；空白或只剩分隔符號時拒絕。"""
    if not isinstance(raw, str):
        raise TypeError("filename 必須是字串")
    name = PurePath(raw.replace("\\", "/")).name.strip()
    if not name or name in (".", ".."):
        raise ValueError(f"filename 不合法：{raw!r}")
    check_fields({"filename": name})
    return name


@dataclass(frozen=True)
class UploadResult:
    document: Document
    space: str
    duplicate: bool = False
    retried: bool = False

    def to_dict(self) -> dict[str, Any]:
        doc = self.document
        return {
            "document_id": doc.id,
            "status": doc.status,
            "sha256": doc.sha256,
            "duplicate": self.duplicate,
            "retried": self.retried,
            "vault": doc.vault,
            "space": self.space,
            "filename": doc.filename,
            "version": doc.version,
            "supersedes": doc.supersedes,
            "size_bytes": doc.size_bytes,
        }


def upload(
    conn: sqlite3.Connection,
    blobs: BlobStore,
    vault: str,
    data: bytes,
    *,
    space: str,
    filename: str,
    mime: str | None = None,
    max_bytes: int,
) -> UploadResult:
    """收一份文件：寫 blob、依上表決定回既有／重試／新版本／新文件。"""
    name = clean_filename(filename)
    content_type = (mime or "").strip() or DEFAULT_MIME
    check_fields({"mime": content_type})
    if len(data) > max_bytes:
        raise UploadRejected(
            TOO_LARGE, f"檔案 {len(data)} 位元組，超過上限 {max_bytes}"
        )
    try:
        detect_format(name, content_type)
    except ExtractionError as exc:
        raise UploadRejected(exc.code, exc.detail) from None
    # 範圍錯誤不該先寫 blob
    resolve_write(conn, vault, space=space)
    sha = sha256_bytes(data)
    blobs.put(data)
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        same = store.find_by_sha256(conn, key, sha, space=space)
        superseded = store.superseded_by(conn, [d.id for d in same])
        live = [d for d in same if d.id not in superseded]
        active = [d for d in live if d.status != store.STATUS_FAILED]
        if active:
            return UploadResult(active[0], space, duplicate=True)
        if live:
            # 同內容的現行列都是失敗紀錄：沿用最新那一列重跑
            doc = store.reset_for_retry(
                conn, live[0].id, filename=name, mime=content_type
            )
            return UploadResult(doc, space, retried=True)
        previous, max_version = store.latest_live_by_filename(conn, key, name)
        doc = store.insert_document(
            conn,
            key,
            space=space,
            filename=name,
            mime=content_type,
            size_bytes=len(data),
            sha256=sha,
            supersedes=previous.id if previous is not None else None,
            version=max_version + 1 if max_version else None,
        )
        return UploadResult(doc, space)


# ── get／list 用的文件項目 ────────────────────────────────────────────


def is_document_ref(value: str) -> bool:
    return isinstance(value, str) and (
        value.startswith(store.DOCUMENT_ID_PREFIX) or parse_chunk_id(value) is not None
    )


def _documents_in_scope(
    conn: sqlite3.Connection, vault: str, ids: Sequence[str], *, space: str
) -> dict[str, Document]:
    return {d.id: d for d in store.get_documents(conn, vault, list(ids), space=space)}


def resolve_refs(
    conn: sqlite3.Connection, vault: str, refs: Sequence[str], *, space: str
) -> dict[str, dict[str, Any]]:
    """文件／chunk id → 未套預算的項目（含全文 `text`）；範圍外或不存在的不回。"""
    doc_ids = {r for r in refs if r.startswith(store.DOCUMENT_ID_PREFIX)}
    chunk_keys = {r: key for r in refs if (key := parse_chunk_id(r)) is not None}
    documents = _documents_in_scope(
        conn, vault, sorted(doc_ids | {k[0] for k in chunk_keys.values()}), space=space
    )
    replaced = store.superseded_by(conn, list(documents))
    chunks = chunks_by_key(conn, [k for k in chunk_keys.values() if k[0] in documents])
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = chunk_keys.get(ref)
        if key is not None:
            chunk = chunks.get(key)
            doc = documents.get(key[0])
            if chunk is None or doc is None:
                continue
            result[ref] = {
                "id": chunk_id(doc.id, chunk.idx),
                "kind": KIND_CHUNK,
                "document_id": doc.id,
                "vault": doc.vault,
                "title": doc.filename,
                "locator": chunk.locator,
                "text": chunk.text,
                "superseded_by": replaced.get(doc.id),
                "updated": doc.updated,
            }
            continue
        doc = documents.get(ref)
        if doc is None:
            continue
        text = join_chunks((c.text, c.overlap) for c in chunks_of(conn, doc.id))
        result[ref] = {
            **document_summary(doc, replaced.get(doc.id)),
            "text": text,
        }
    return result


def document_summary(doc: Document, superseded_by: str | None) -> dict[str, Any]:
    """list 與 get 共用的文件 metadata（不含全文）。"""
    return {
        "id": doc.id,
        "kind": KIND_DOCUMENT,
        "vault": doc.vault,
        "title": doc.filename,
        "filename": doc.filename,
        "mime": doc.mime,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error_code": doc.error_code,
        "error_detail": doc.error_detail,
        "version": doc.version,
        "supersedes": doc.supersedes,
        "superseded_by": superseded_by,
        "chunk_count": doc.chunk_count,
        "encoding": doc.encoding,
        "created": doc.created,
        "updated": doc.updated,
    }


def list_page(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    since: str | None,
    limit: int,
    cursor: tuple[str, str] | None,
) -> tuple[list[tuple[str, str, dict[str, Any]]], tuple[str, str] | None]:
    """文件的一頁 list 項目：[(updated, id, 項目)]、下一頁 cursor。"""
    docs, next_cursor = store.list_documents_since(
        conn, vault, space=space, since=since, limit=limit, cursor=cursor
    )
    replaced = store.superseded_by(conn, [d.id for d in docs])
    return [
        (d.updated, d.id, document_summary(d, replaced.get(d.id))) for d in docs
    ], next_cursor
