"""文件 metadata 的儲存原語（A19，T-58）。

- document 必屬某個 vault；space 由 vault 決定。所有讀取都經
  `resolve_read` + `vault_clause`、寫入經 `resolve_write`，`space` 為必填參數，
  與 note 同一道硬過濾（A5／A18）：vault 在別的 space → `UnknownVault`。
- 本模組只管 `documents` 列本身；原始檔在 `storage.blobs`，抽取、切段、
  狀態轉換與索引寫入屬 T-63 之後。
- 純標準庫（不 import numpy），doctor 的唯讀對帳也能用。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from lore_vault.schema.chars import check_fields

from .db import transaction
from .errors import NotFound
from .migrate import DOCUMENT_ERROR_CODES
from .timeutil import utc_now
from .vaults import resolve_read, resolve_write, vault_clause

DOCUMENT_ID_PREFIX = "doc:"

STATUS_PENDING = "pending"
STATUS_EXTRACTING = "extracting"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
DOCUMENT_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_EXTRACTING, STATUS_READY, STATUS_FAILED}
)

ERROR_CODES = frozenset(DOCUMENT_ERROR_CODES)

_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Document:
    id: str
    vault: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    version: int
    supersedes: str | None
    status: str
    error_code: str | None
    error_detail: str | None
    chunk_count: int
    created: str
    updated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "vault": self.vault,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "version": self.version,
            "supersedes": self.supersedes,
            "status": self.status,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "chunk_count": self.chunk_count,
            "created": self.created,
            "updated": self.updated,
        }


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        vault=row["vault"],
        filename=row["filename"],
        mime=row["mime"],
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        version=int(row["version"]),
        supersedes=row["supersedes"],
        status=row["status"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        chunk_count=int(row["chunk_count"]),
        created=row["created"],
        updated=row["updated"],
    )


def validate_sha256(value: object) -> str:
    """sha256 必須是 64 字元小寫十六進位（blob 路徑也由它組成，先驗再用）。"""
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _SHA256_HEX:
        raise ValueError(f"sha256 必須是 64 字元小寫十六進位，得到 {value!r}")
    return value


def new_document_id() -> str:
    return f"{DOCUMENT_ID_PREFIX}{uuid.uuid4()}"


def insert_document(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    filename: str,
    mime: str,
    size_bytes: int,
    sha256: str,
    supersedes: str | None = None,
    document_id: str | None = None,
) -> Document:
    """新增一列 `status='pending'` 的 document，回傳存下的版本。

    - `supersedes`：取代同 vault 內的既有 document；新列 version = 舊 version + 1。
      舊列不存在或屬於別的 vault → `NotFound`。
    - 不做同 vault 同雜湊去重（上傳端的語意，T-67），也不檢查 blob 是否已寫入
      （呼叫端先寫 blob 再建列；doctor `documents.blob_exists` 對帳）。
    """
    check_fields({"filename": filename, "mime": mime})
    if not filename.strip():
        raise ValueError("filename 不可為空")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise TypeError("size_bytes 必須是整數")
    if size_bytes < 0:
        raise ValueError("size_bytes 不可為負")
    validate_sha256(sha256)
    doc_id = document_id if document_id is not None else new_document_id()
    if not doc_id.startswith(DOCUMENT_ID_PREFIX) or doc_id == DOCUMENT_ID_PREFIX:
        raise ValueError(f"document id 必須以 {DOCUMENT_ID_PREFIX!r} 開頭：{doc_id!r}")
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        version = 1
        if supersedes is not None:
            row = conn.execute(
                "SELECT version FROM documents WHERE id = ? AND vault = ?",
                (supersedes, key),
            ).fetchone()
            if row is None:
                raise NotFound(
                    f"vault {key!r} 內找不到要取代的 document {supersedes!r}"
                )
            version = int(row[0]) + 1
        now = utc_now()
        conn.execute(
            """
            INSERT INTO documents (id, vault, filename, mime, size_bytes, sha256,
                                   version, supersedes, status, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                key,
                filename,
                mime,
                size_bytes,
                sha256,
                version,
                supersedes,
                STATUS_PENDING,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return _row_to_document(row)


def get_document(
    conn: sqlite3.Connection, vault: str, document_id: str, *, space: str
) -> Document:
    """在 vault（與 space）範圍內取 document；不在範圍內一律 `NotFound`。"""
    scope = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope, "vault")
    row = conn.execute(
        f"SELECT * FROM documents WHERE id = ? AND {clause}",
        (document_id, *params),
    ).fetchone()
    if row is None:
        raise NotFound(f"找不到 document {document_id!r}")
    return _row_to_document(row)


def find_by_sha256(
    conn: sqlite3.Connection, vault: str, sha256: str, *, space: str
) -> list[Document]:
    """範圍內同雜湊的 document（由新到舊），供上傳去重判斷。"""
    validate_sha256(sha256)
    scope = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope, "vault")
    rows = conn.execute(
        f"""
        SELECT * FROM documents WHERE sha256 = ? AND {clause}
        ORDER BY created DESC, id DESC
        """,
        (sha256, *params),
    ).fetchall()
    return [_row_to_document(r) for r in rows]


def list_documents(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    status: str | None = None,
    limit: int = 50,
    cursor: tuple[str, str] | None = None,
) -> tuple[list[Document], tuple[str, str] | None]:
    """依 (updated, id) 由新到舊分頁；回傳 (本頁, 下一頁 cursor 或 None)。

    條件都在 SQL 內、LIMIT 之前套用，分頁不會因事後過濾而少回。
    """
    scope = resolve_read(conn, vault, space=space)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if status is not None:
        if status not in DOCUMENT_STATUSES:
            raise ValueError(
                f"status 必須是 {sorted(DOCUMENT_STATUSES)} 之一，得到 {status!r}"
            )
        conditions.append("status = ?")
        args.append(status)
    if cursor is not None:
        conditions.append("(updated, id) < (?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT * FROM documents WHERE {" AND ".join(conditions)}
        ORDER BY updated DESC, id DESC
        LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = [_row_to_document(r) for r in rows[:limit]]
    next_cursor = (page[-1].updated, page[-1].id) if len(rows) > limit else None
    return page, next_cursor


def referenced_sha256(conn: sqlite3.Connection) -> set[str]:
    """所有 document 列引用的 blob 雜湊（不分 vault／space；對帳用）。"""
    return {r[0] for r in conn.execute("SELECT DISTINCT sha256 FROM documents")}
