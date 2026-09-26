"""管理操作：刪單則 note、刪整個 vault、換 space（改 key）、復原文件。

經管理指令（`cli.admin`）與 UI 管理端點（`api.manage`，兩段式確認）；不提供 MCP 工具。

每個刪除先「規劃」（只讀、列出會刪的筆數與 id），實際刪除在單一交易內重新規劃再刪，
並核對實際刪除筆數與規劃一致，不一致整段 rollback。

連帶資料：
- note：FTS 列（虛擬表無外鍵，手動刪）、向量與補算紀錄（外鍵 CASCADE）
- vault：上述 + 別名（CASCADE）+ episodes／concepts／injections
  （外鍵無 CASCADE，手動刪）+ 文件（見下）
- 文件（T-68）：chunk_fts 列（手動刪）、chunk（手動刪；向量隨 chunk CASCADE）、
  抽取／補算紀錄（隨文件 CASCADE），並寫 `document_tombstones`。blob **不刪**：
  可能被其他 vault 或其他版本引用；沒人引用時由 doctor `documents.orphan_blobs`
  回報，清理是另一個明確操作。刪單一文件時，以 `supersedes` 指向它的新版本改指向
  它的前一版（版本鏈不斷），並在同一交易內重算前後版本的索引資格（刪掉現行版本時，
  前一版回到索引；向量由 worker 補算）。
- 墓碑（`note_tombstones`，schema v5）：每則被刪的 note 寫一筆墓碑（id、vault、
  對帳清單記載的來源、刪除時間、原因）。匯入對帳清單（`import_sources`／
  `import_vault_counts`）**不動**：對帳把「清單有、note 沒有、有墓碑」算成刻意刪除，
  沒有墓碑的才是漏匯；重跑匯入遇到墓碑跳過，不會把刻意刪掉的 note 匯回來。
  v12 起墓碑另存刪除當下 note 的完整內容（`snapshot`，JSON）：`restore_note` 以原 id
  與原內容還原（FTS 同交易重建，向量交給背景補算）；v12 前沒有快照的舊墓碑維持
  `undelete_note` 的舊行為（只移除墓碑，下次匯入即可匯回）。

墓碑清除（`purge_tombstones`，`cli.admin purge-tombstones`）：永久刪除刪除時間早於門檻的
note 墓碑（連同內容快照）與文件墓碑。清除後無法還原（undelete 得 404），也不再擋重跑匯入
（被刪的匯入 note 會匯回來）。為了讓匯入對帳維持一致，同一交易內一併移除這些 note 的對帳
清單列（`import_sources`）並把該來源、該 vault 的來源筆數（`import_vault_counts`）減一——
對帳把它們當成「從未匯入過」；重跑匯入時 `record_manifest` 會整批重建清單與筆數。
文件墓碑的原始檔 blob 本來就不算引用（只看 documents），清除後由 `gc-blobs` 回收。
不做自動清除。

換 space（A20）：只允許 `lore`↔`personal`，dev 與非 dev 兩個方向都拒絕。換 space 同時把
key 與別名改成新前綴，引用 vault key 的欄位全部在同一交易內改寫（舊 key 不留別名）。
引用欄位由 schema 動態列出（`vault_reference_columns`：欄名為 `vault` 或外鍵指向
`vaults`），日後新增的表只要沿用任一慣例就不會漏改；已知清單
`KNOWN_VAULT_REFERENCES` 中存在的表若沒被偵測到，視為偵測失效、拒絕執行。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from lore_vault.schema import SPACE_DEV, Note, canonical_key

from . import document_index
from .db import transaction
from .errors import NotFound, StorageError, UnknownVault, VaultConflict
from .notes import insert_note, note_from_row
from .timeutil import format_utc, parse_utc, utc_now
from .vaults import check_key_prefix, resolve_write, validate_space

DEFAULT_NOTE_REASON = "admin delete-note"
DEFAULT_VAULT_REASON = "admin delete-vault"
DEFAULT_DOCUMENT_REASON = "admin delete-document"


class NeedsForce(StorageError):
    """vault 內還有資料，未加 force 不刪。"""


class PlanChanged(StorageError):
    """實際刪除筆數與規劃不一致（規劃後資料被改動）；已 rollback。"""


@dataclass(frozen=True)
class DeletePlan:
    """將刪除內容的 metadata（只有 id 與筆數，不含標題、內文）。"""

    target: str
    vault: str
    note_ids: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)
    # 需要 force 才能刪（vault 內仍有 note 或其他紀錄）
    requires_force: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "vault": self.vault,
            "counts": dict(self.counts),
            "note_ids": list(self.note_ids),
            "requires_force": self.requires_force,
        }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ── 單則 note ──


def plan_note_deletion(
    conn: sqlite3.Connection, vault: str, note_id: str, *, space: str
) -> DeletePlan:
    key = resolve_write(conn, vault, space=space)
    row = conn.execute(
        "SELECT seq FROM notes WHERE id = ? AND vault = ?", (note_id, key)
    ).fetchone()
    if row is None:
        raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
    seq = int(row["seq"])
    counts = {
        "notes": 1,
        "fts_rows": _count(
            conn, "SELECT count(*) FROM note_fts WHERE rowid = ?", (seq,)
        ),
        "embeddings": _count(
            conn, "SELECT count(*) FROM note_embeddings WHERE note_seq = ?", (seq,)
        ),
        "enrichment": _count(
            conn, "SELECT count(*) FROM note_enrichment WHERE note_seq = ?", (seq,)
        ),
        "tombstones": 1,
    }
    return DeletePlan(target="note", vault=key, note_ids=(note_id,), counts=counts)


def delete_note(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    *,
    space: str,
    reason: str = DEFAULT_NOTE_REASON,
) -> DeletePlan:
    """刪一則 note 與其 FTS、向量、補算紀錄，並寫墓碑（單一交易）。"""
    with transaction(conn):
        plan = plan_note_deletion(conn, vault, note_id, space=space)
        seq = conn.execute(
            "SELECT seq FROM notes WHERE id = ? AND vault = ?", (note_id, plan.vault)
        ).fetchone()["seq"]
        done = {"tombstones": _write_tombstones(conn, plan.vault, [note_id], reason)}
        done["fts_rows"] = conn.execute(
            "DELETE FROM note_fts WHERE rowid = ?", (seq,)
        ).rowcount
        done["embeddings"] = plan.counts["embeddings"]
        done["enrichment"] = plan.counts["enrichment"]
        done["notes"] = conn.execute("DELETE FROM notes WHERE seq = ?", (seq,)).rowcount
        _check_cascade(conn)
        _verify(plan, done)
        return plan


def _write_tombstones(
    conn: sqlite3.Connection, vault: str, note_ids: Sequence[str], reason: str
) -> int:
    """每則 note 寫一筆墓碑；來源取自對帳清單（沒有就 NULL）。回傳寫入筆數。

    v12 起同時存 note 完整內容的快照（`snapshot`），`restore_note` 據此還原。
    """
    if not reason.strip():
        raise StorageError("刪除原因不可為空")
    has_manifest = _has_table(conn, "import_sources")
    has_snapshot = _has_column(conn, "note_tombstones", "snapshot")
    now = utc_now()
    written = 0
    for note_id in note_ids:
        source = source_id = None
        if has_manifest:
            row = conn.execute(
                "SELECT source, source_id FROM import_sources WHERE note_id = ?",
                (note_id,),
            ).fetchone()
            if row is not None:
                source, source_id = row[0], row[1]
        if has_snapshot:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                raise PlanChanged(f"note {note_id!r} 已不存在，無法寫墓碑快照")
            written += conn.execute(
                """
                INSERT OR REPLACE INTO note_tombstones
                    (note_id, vault, source, source_id, deleted_at, reason, snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    vault,
                    source,
                    source_id,
                    now,
                    reason,
                    json.dumps(note_from_row(row).to_dict(), ensure_ascii=False),
                ),
            ).rowcount
            continue
        # 取消刪除後再刪一次：覆寫舊墓碑
        written += conn.execute(
            """
            INSERT OR REPLACE INTO note_tombstones
                (note_id, vault, source, source_id, deleted_at, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (note_id, vault, source, source_id, now, reason),
        ).rowcount
    return written


def find_tombstone(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    """查一則墓碑（只有 metadata）；沒有就 NotFound。"""
    row = conn.execute(
        """
        SELECT note_id, vault, source, source_id, deleted_at, reason
        FROM note_tombstones WHERE note_id = ?
        """,
        (note_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"note {note_id!r} 沒有墓碑")
    return {
        "note_id": row[0],
        "vault": row[1],
        "source": row[2],
        "source_id": row[3],
        "deleted_at": row[4],
        "reason": row[5],
        # 有快照（v12 起刪除的）才能以原內容還原；只回旗標，不回內容
        "has_snapshot": _tombstone_snapshot(conn, note_id) is not None,
    }


def _tombstone_snapshot(conn: sqlite3.Connection, note_id: str) -> str | None:
    if not _has_column(conn, "note_tombstones", "snapshot"):
        return None
    row = conn.execute(
        "SELECT snapshot FROM note_tombstones WHERE note_id = ?", (note_id,)
    ).fetchone()
    return None if row is None else row[0]


def undelete_note(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    """只移除墓碑（不還原內容），下次匯入可把該 note 匯回來。回傳被移除的墓碑。

    v12 前沒有快照的舊墓碑走這條；有快照的請用 `restore_note`。
    """
    with transaction(conn):
        grave = find_tombstone(conn, note_id)
        conn.execute("DELETE FROM note_tombstones WHERE note_id = ?", (note_id,))
        return grave


def restore_note(
    conn: sqlite3.Connection, note_id: str, *, space: str | None = None
) -> dict[str, Any]:
    """取消刪除：墓碑有快照就以原 id 與原內容還原，沒有（v12 前的舊墓碑）就只移除墓碑。

    - 還原：title、body、summary、topics、links、supersedes、作者欄位、created、
      updated 與刪除前逐欄相同；FTS 在同一交易重建，向量已隨刪除消失，交給背景補算
    - vault 取墓碑的 `vault` 欄（換 space 會改寫它，快照 JSON 內的 vault 可能已過時）
    - 墓碑所屬 vault 已刪除 → `NotRestorable("vault_deleted")`，墓碑保留；
      `space` 給定時 vault 必須在該 space（否則 `UnknownVault`），
      省略用 vault 目前的 space
    - 同 id 的 note 已存在 → `NotRestorable("exists")`
    - 刪墓碑與插入在同一交易
    回傳 {"tombstone": 墓碑 dict, "restored": bool, "note": Note | None}。
    """
    with transaction(conn):
        grave = find_tombstone(conn, note_id)
        raw = _tombstone_snapshot(conn, note_id)
        if raw is None:
            conn.execute("DELETE FROM note_tombstones WHERE note_id = ?", (note_id,))
            return {"tombstone": grave, "restored": False, "note": None}
        row = conn.execute(
            "SELECT space FROM vaults WHERE key = ?", (grave["vault"],)
        ).fetchone()
        if row is None:
            raise NotRestorable(
                f"note {note_id!r} 所屬的 vault {grave['vault']!r} 已刪除；"
                "請先建立同一個 vault 再取消刪除",
                "vault_deleted",
            )
        key = resolve_write(conn, grave["vault"], space=space or row[0])
        if conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone():
            raise NotRestorable(f"note {note_id!r} 已存在", "exists")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("id") != note_id:
            raise StorageError(f"note {note_id!r} 的墓碑快照與墓碑 id 不符")
        note = dataclasses.replace(Note.from_dict(data), vault=key)
        conn.execute("DELETE FROM note_tombstones WHERE note_id = ?", (note_id,))
        stored = insert_note(conn, key, note, space=space or row[0])
        return {"tombstone": grave, "restored": True, "note": stored}


# ── 單份文件（T-68）──


@dataclass(frozen=True)
class DocumentDeletePlan:
    """將刪除的文件 metadata（不含檔名以外的內容）。"""

    document_id: str
    vault: str
    sha256: str
    filename: str
    supersedes: str | None
    # 以 supersedes 指向它、刪除後改指向它前一版的新版本
    relinked: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)
    # 刪除後 blob 仍被其他文件引用（False＝變成孤兒，doctor 會回報）
    blob_still_referenced: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": "document",
            "document_id": self.document_id,
            "vault": self.vault,
            "sha256": self.sha256,
            "filename": self.filename,
            "supersedes": self.supersedes,
            "relinked": list(self.relinked),
            "counts": dict(self.counts),
            "blob_still_referenced": self.blob_still_referenced,
        }


def _document_counts(conn: sqlite3.Connection, where: str, params: tuple) -> dict:
    """`where` 篩選 documents（別名 d）；回傳連帶資料筆數。"""
    docs = f"SELECT d.id FROM documents d WHERE {where}"
    chunks = f"SELECT seq FROM document_chunks WHERE document_id IN ({docs})"
    return {
        "documents": _count(conn, f"SELECT count(*) FROM ({docs})", params),
        "chunks": _count(conn, f"SELECT count(*) FROM ({chunks})", params),
        "chunk_fts_rows": _count(
            conn, f"SELECT count(*) FROM chunk_fts WHERE rowid IN ({chunks})", params
        ),
        "chunk_embeddings": _count(
            conn,
            f"SELECT count(*) FROM document_chunk_embeddings "
            f"WHERE chunk_seq IN ({chunks})",
            params,
        ),
        "document_enrichment": _count(
            conn,
            f"SELECT count(*) FROM document_enrichment WHERE document_id IN ({docs})",
            params,
        ),
    }


def plan_document_deletion(
    conn: sqlite3.Connection, vault: str, document_id: str, *, space: str
) -> DocumentDeletePlan:
    key = resolve_write(conn, vault, space=space)
    row = conn.execute(
        "SELECT id, sha256, filename, supersedes FROM documents "
        "WHERE id = ? AND vault = ?",
        (document_id, key),
    ).fetchone()
    if row is None:
        raise NotFound(f"vault {key!r} 內找不到文件 {document_id!r}")
    relinked = tuple(
        r[0]
        for r in conn.execute(
            "SELECT id FROM documents WHERE supersedes = ? ORDER BY id", (document_id,)
        )
    )
    counts = _document_counts(conn, "d.id = ?", (document_id,))
    counts["tombstones"] = 1
    others = _count(
        conn,
        "SELECT count(*) FROM documents WHERE sha256 = ? AND id != ?",
        (row["sha256"], document_id),
    )
    return DocumentDeletePlan(
        document_id=document_id,
        vault=key,
        sha256=row["sha256"],
        filename=row["filename"],
        supersedes=row["supersedes"],
        relinked=relinked,
        counts=counts,
        blob_still_referenced=others > 0,
    )


def _write_document_tombstones(
    conn: sqlite3.Connection, where: str, params: tuple, reason: str
) -> int:
    if not reason.strip():
        raise StorageError("刪除原因不可為空")
    if _has_column(conn, "document_tombstones", "filename"):
        # v11：一併記下重建 documents 列所需的 metadata（undelete_document 用）
        return conn.execute(
            f"""
            INSERT OR REPLACE INTO document_tombstones
                (document_id, vault, sha256, deleted_at, reason,
                 filename, mime, size_bytes, version)
            SELECT d.id, d.vault, d.sha256, ?, ?,
                   d.filename, d.mime, d.size_bytes, d.version
            FROM documents d WHERE {where}
            """,
            (utc_now(), reason, *params),
        ).rowcount
    return conn.execute(
        f"""
        INSERT OR REPLACE INTO document_tombstones
            (document_id, vault, sha256, deleted_at, reason)
        SELECT d.id, d.vault, d.sha256, ?, ? FROM documents d WHERE {where}
        """,
        (utc_now(), reason, *params),
    ).rowcount


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _delete_documents(conn: sqlite3.Connection, where: str, params: tuple) -> dict:
    """刪 chunk_fts、chunk（向量 CASCADE）、文件（補算紀錄 CASCADE）。"""
    docs = f"SELECT d.id FROM documents d WHERE {where}"
    chunks = f"SELECT seq FROM document_chunks WHERE document_id IN ({docs})"
    done = {
        "chunk_fts_rows": conn.execute(
            f"DELETE FROM chunk_fts WHERE rowid IN ({chunks})", params
        ).rowcount,
    }
    done["chunks"] = conn.execute(
        f"DELETE FROM document_chunks WHERE document_id IN ({docs})", params
    ).rowcount
    done["documents"] = conn.execute(
        f"DELETE FROM documents WHERE id IN ({docs})", params
    ).rowcount
    return done


def delete_document(
    conn: sqlite3.Connection,
    vault: str,
    document_id: str,
    *,
    space: str,
    reason: str = DEFAULT_DOCUMENT_REASON,
) -> DocumentDeletePlan:
    """刪一份文件與其 chunk、FTS、向量、抽取紀錄，並寫墓碑（單一交易）。"""
    with transaction(conn):
        plan = plan_document_deletion(conn, vault, document_id, space=space)
        # 版本鏈不斷：指向它的新版本改指向它的前一版
        conn.execute(
            "UPDATE documents SET supersedes = ? WHERE supersedes = ?",
            (plan.supersedes, document_id),
        )
        done = {
            "tombstones": _write_document_tombstones(
                conn, "d.id = ?", (document_id,), reason
            )
        }
        done.update(_delete_documents(conn, "d.id = ?", (document_id,)))
        done["chunk_embeddings"] = plan.counts["chunk_embeddings"]
        done["document_enrichment"] = plan.counts["document_enrichment"]
        _check_cascade(conn)
        _verify_counts(plan.counts, done)
        # 前後版本的索引資格可能改變（刪掉現行版本 → 前一版回到索引）
        for newer in plan.relinked:
            document_index.sync_chain(conn, newer)
        if plan.supersedes is not None:
            document_index.sync_chain(conn, plan.supersedes)
        return plan


class NotRestorable(StorageError):
    """墓碑缺重建所需的資料（v11 前的舊墓碑、原始檔遺失、同內容已存在等）。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def find_document_tombstone(
    conn: sqlite3.Connection, document_id: str
) -> dict[str, Any]:
    """查一則文件墓碑（只有 metadata）；沒有就 NotFound。"""
    row = conn.execute(
        "SELECT * FROM document_tombstones WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"文件 {document_id!r} 沒有墓碑")
    keys = row.keys()
    return {
        "document_id": row["document_id"],
        "vault": row["vault"],
        "sha256": row["sha256"],
        "deleted_at": row["deleted_at"],
        "reason": row["reason"],
        "filename": row["filename"] if "filename" in keys else None,
        "mime": row["mime"] if "mime" in keys else None,
        "size_bytes": row["size_bytes"] if "size_bytes" in keys else None,
        "version": row["version"] if "version" in keys else None,
    }


def undelete_document(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    space: str,
    blob_ok: Callable[[str], bool],
) -> dict[str, Any]:
    """以墓碑 metadata 與仍在的原始檔重建文件（同一 id），狀態回到 pending 重新抽取。

    - 墓碑所屬 vault 已刪除 → `NotRestorable("vault_deleted")`；在別的 space →
      `UnknownVault`（呼叫端應先以墓碑 space 過濾，別讓訊息帶出別 space 的 key）
    - 舊墓碑（v11 前，無 filename／mime／size）→ `NotRestorable(reason="incomplete")`
    - `blob_ok(sha256) -> bool`：原始檔存在且雜湊相符；否則
      `NotRestorable("blob_missing")`
    - 同 vault 已有相同內容的現行文件 → `NotRestorable("duplicate")`（不重複建）
    - 版本鏈比照上傳：同檔名現行版本為 `supersedes`、version = 該檔名最大版本 + 1
    - 刪墓碑與建列在同一交易
    回傳 {"document": Document, "tombstone": 墓碑 dict}。
    """
    from . import documents as store

    with transaction(conn):
        grave = find_document_tombstone(conn, document_id)
        if not conn.execute(
            "SELECT 1 FROM vaults WHERE key = ?", (grave["vault"],)
        ).fetchone():
            raise NotRestorable(
                f"文件 {document_id!r} 所屬的 vault 已刪除；請先建立 vault 再重新上傳",
                "vault_deleted",
            )
        key = resolve_write(conn, grave["vault"], space=space)
        if grave["filename"] is None or grave["mime"] is None:
            raise NotRestorable(
                f"文件 {document_id!r} 的墓碑缺檔名／格式（v11 前刪除）；請重新上傳",
                "incomplete",
            )
        if grave["size_bytes"] is None:
            raise NotRestorable(
                f"文件 {document_id!r} 的墓碑缺檔案大小；請重新上傳", "incomplete"
            )
        if conn.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone():
            raise NotRestorable(f"文件 {document_id!r} 已存在", "exists")
        if not blob_ok(grave["sha256"]):
            raise NotRestorable(
                f"文件 {document_id!r} 的原始檔已不存在或損毀"
                "（可能已被 gc-blobs 清掉）",
                "blob_missing",
            )
        same = store.find_by_sha256(conn, key, grave["sha256"], space=space)
        superseded = store.superseded_by(conn, [d.id for d in same])
        live = [d for d in same if d.id not in superseded]
        if live:
            raise NotRestorable(
                f"vault 內已有相同內容的文件 {live[0].id!r}", "duplicate"
            )
        previous, max_version = store.latest_live_by_filename(
            conn, key, grave["filename"]
        )
        conn.execute(
            "DELETE FROM document_tombstones WHERE document_id = ?", (document_id,)
        )
        doc = store.insert_document(
            conn,
            key,
            space=space,
            filename=grave["filename"],
            mime=grave["mime"],
            size_bytes=int(grave["size_bytes"]),
            sha256=grave["sha256"],
            supersedes=previous.id if previous is not None else None,
            version=max_version + 1 if max_version else None,
            document_id=document_id,
        )
        return {"document": doc, "tombstone": grave}


# ── 整個 vault ──

# 參照 vaults(key) 但沒有 ON DELETE CASCADE 的表
_VAULT_RECORD_TABLES = ("episodes", "concepts", "injections")


def plan_vault_deletion(conn: sqlite3.Connection, key: str) -> DeletePlan:
    """只接受 vault 正式 key（不接受別名，避免誤刪）。"""
    row = conn.execute("SELECT key FROM vaults WHERE key = ?", (key,)).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (key,)
        ).fetchone()
        hint = f"（這是 {alias[0]!r} 的別名；請用正式 key）" if alias else ""
        raise UnknownVault(f"vault 不存在：{key!r}{hint}")
    note_ids = tuple(
        r[0]
        for r in conn.execute(
            "SELECT id FROM notes WHERE vault = ? ORDER BY seq", (key,)
        )
    )
    seqs = "SELECT seq FROM notes WHERE vault = ?"
    counts = {
        "notes": len(note_ids),
        "fts_rows": _count(
            conn, f"SELECT count(*) FROM note_fts WHERE rowid IN ({seqs})", (key,)
        ),
        "embeddings": _count(
            conn,
            f"SELECT count(*) FROM note_embeddings WHERE note_seq IN ({seqs})",
            (key,),
        ),
        "enrichment": _count(
            conn,
            f"SELECT count(*) FROM note_enrichment WHERE note_seq IN ({seqs})",
            (key,),
        ),
        "aliases": _count(
            conn, "SELECT count(*) FROM vault_aliases WHERE vault = ?", (key,)
        ),
        "tombstones": len(note_ids),
    }
    for table in _VAULT_RECORD_TABLES:
        counts[table] = _count(
            conn, f"SELECT count(*) FROM {table} WHERE vault = ?", (key,)
        )
    if _has_table(conn, "documents"):
        doc_counts = _document_counts(conn, "d.vault = ?", (key,))
        counts.update(doc_counts)
        counts["document_tombstones"] = doc_counts["documents"]
    requires_force = (
        counts["notes"] > 0
        or counts.get("documents", 0) > 0
        or any(counts[t] > 0 for t in _VAULT_RECORD_TABLES)
    )
    return DeletePlan(
        target="vault",
        vault=key,
        note_ids=note_ids,
        counts=counts,
        requires_force=requires_force,
    )


def delete_vault(
    conn: sqlite3.Connection,
    key: str,
    *,
    force: bool = False,
    reason: str = DEFAULT_VAULT_REASON,
) -> DeletePlan:
    """刪 vault 與其全部資料，並為其下每則 note 寫墓碑（單一交易）。

    vault 內有資料時必須 `force=True`。
    """
    with transaction(conn):
        plan = plan_vault_deletion(conn, key)
        if plan.requires_force and not force:
            raise NeedsForce(
                f"vault {key!r} 內還有 {plan.counts['notes']} 則 note 或其他紀錄；"
                "確定要一併刪除請加 --force"
            )
        seqs = "SELECT seq FROM notes WHERE vault = ?"
        done: dict[str, int] = {
            "tombstones": _write_tombstones(conn, key, plan.note_ids, reason)
        }
        done["fts_rows"] = conn.execute(
            f"DELETE FROM note_fts WHERE rowid IN ({seqs})", (key,)
        ).rowcount
        # 向量、補算紀錄隨 notes 以 CASCADE 刪除；先記下規劃值再核對剩餘
        done["embeddings"] = plan.counts["embeddings"]
        done["enrichment"] = plan.counts["enrichment"]
        done["notes"] = conn.execute(
            "DELETE FROM notes WHERE vault = ?", (key,)
        ).rowcount
        for table in _VAULT_RECORD_TABLES:
            done[table] = conn.execute(
                f"DELETE FROM {table} WHERE vault = ?", (key,)
            ).rowcount
        if "documents" in plan.counts:
            done["document_tombstones"] = _write_document_tombstones(
                conn, "d.vault = ?", (key,), reason
            )
            # 同 vault 內的版本鏈一起刪：先解開自我參照再刪
            conn.execute(
                "UPDATE documents SET supersedes = NULL WHERE vault = ?", (key,)
            )
            done.update(_delete_documents(conn, "d.vault = ?", (key,)))
            done["chunk_embeddings"] = plan.counts["chunk_embeddings"]
            done["document_enrichment"] = plan.counts["document_enrichment"]
        done["aliases"] = plan.counts["aliases"]
        if conn.execute("DELETE FROM vaults WHERE key = ?", (key,)).rowcount != 1:
            raise PlanChanged(f"vault {key!r} 刪除失敗")
        leftover = _count(
            conn, "SELECT count(*) FROM vault_aliases WHERE vault = ?", (key,)
        )
        if leftover:
            raise PlanChanged(f"vault {key!r} 仍有 {leftover} 個別名未刪")
        _check_cascade(conn)
        _verify(plan, done)
        return plan


_CASCADE_CHECKS = (
    ("note_embeddings", "note_seq NOT IN (SELECT seq FROM notes)"),
    ("note_enrichment", "note_seq NOT IN (SELECT seq FROM notes)"),
    (
        "document_chunk_embeddings",
        "chunk_seq NOT IN (SELECT seq FROM document_chunks)",
    ),
    ("document_enrichment", "document_id NOT IN (SELECT id FROM documents)"),
)


def _check_cascade(conn: sqlite3.Connection) -> None:
    """向量與補算紀錄靠外鍵 CASCADE 刪除；外鍵沒開時這裡會抓到孤兒列並 rollback。"""
    for table, condition in _CASCADE_CHECKS:
        if not _has_table(conn, table):
            continue
        orphans = _count(conn, f"SELECT count(*) FROM {table} WHERE {condition}", ())
        if orphans:
            raise PlanChanged(f"{table} 有 {orphans} 列孤兒資料（外鍵 CASCADE 未生效）")


def _verify(plan: DeletePlan, done: dict[str, int]) -> None:
    diff = {
        name: (expected, done.get(name))
        for name, expected in plan.counts.items()
        if done.get(name) != expected
    }
    if diff:
        raise PlanChanged(f"實際刪除筆數與規劃不符：{diff}")


# ── 換 space（A20）──

# 已知引用 vault key 的 (表, 欄)。只當守門用：表存在時動態偵測必須涵蓋它；
# 實際改寫範圍以動態偵測為準（新表自動納入）。
KNOWN_VAULT_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("vault_aliases", "vault"),
        ("notes", "vault"),
        ("episodes", "vault"),
        ("concepts", "vault"),
        ("injections", "vault"),
        ("import_sources", "vault"),
        ("import_vault_counts", "vault"),
        ("note_tombstones", "vault"),
        ("documents", "vault"),
        ("document_tombstones", "vault"),
    }
)


class SpaceChangeRefused(StorageError):
    """A20 不允許的 space 轉換，或 key／別名無法換成新前綴。"""


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ref_name(table: str, column: str) -> str:
    return f"{table}.{column}"


def vault_reference_columns(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """列出 schema 中引用 vault key 的 (表, 欄)：欄名為 `vault`，或外鍵指向 `vaults`。

    有外鍵的表（notes 等）與刻意無外鍵的表（墓碑、匯入對帳清單）都涵蓋。
    """
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'vaults' ORDER BY name"
        )
    ]
    found: set[tuple[str, str]] = set()
    for table in tables:
        for col in conn.execute(f"PRAGMA table_info({_ident(table)})"):
            if col["name"] == "vault":
                found.add((table, "vault"))
        for fk in conn.execute(f"PRAGMA foreign_key_list({_ident(table)})"):
            if fk["table"] == "vaults":
                found.add((table, fk["from"]))
    return tuple(sorted(found))


def _check_detection(
    conn: sqlite3.Connection, columns: Sequence[tuple[str, str]]
) -> None:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    expected = {ref for ref in KNOWN_VAULT_REFERENCES if ref[0] in present}
    missing = expected - set(columns)
    if missing:
        raise StorageError(
            f"引用 vault key 的欄位偵測不完整（缺 {sorted(missing)}）；拒絕改名"
        )


@dataclass(frozen=True)
class SpaceChangePlan:
    """換 space 的規劃：新舊 key、別名對照、各引用欄位受影響筆數。"""

    key: str
    new_key: str
    from_space: str
    to_space: str
    aliases: tuple[tuple[str, str], ...]
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "new_key": self.new_key,
            "from": self.from_space,
            "to": self.to_space,
            "aliases": {old: new for old, new in self.aliases},
            "counts": dict(self.counts),
        }


def _swap_prefix(name: str, old_space: str, new_space: str, what: str) -> str:
    prefix = f"{old_space}/"
    if not name.startswith(prefix) or name == prefix:
        raise SpaceChangeRefused(
            f"{what} {name!r} 不以 '{prefix}' 開頭，無法換成 '{new_space}/' 前綴"
        )
    return f"{new_space}/{name[len(prefix) :]}"


def _name_taken(conn: sqlite3.Connection, name: str) -> str | None:
    """name 已是某 vault 的 key 或別名時回傳說明，否則 None。"""
    if conn.execute("SELECT 1 FROM vaults WHERE key = ?", (name,)).fetchone():
        return f"{name!r} 已是 vault 的 key"
    row = conn.execute(
        "SELECT vault FROM vault_aliases WHERE alias = ?", (name,)
    ).fetchone()
    if row is not None:
        return f"{name!r} 已是 vault {row[0]!r} 的別名"
    return None


def plan_space_change(
    conn: sqlite3.Connection, key: str, space: str, *, new_key: str | None = None
) -> SpaceChangePlan:
    """規劃把 vault（正式 key）在 lore／personal 之間換 space（只讀）。

    - dev 與非 dev 之間兩個方向都拒絕（A20）；目標與目前相同也拒絕
    - 新 key 缺省為把 `<舊 space>/` 換成 `<新 space>/`；給了要符合新前綴
    - 別名一律換前綴，換不了就拒絕；新 key／新別名已被佔用就拒絕
    - 任何引用欄位已有新 key 的資料（殘留）也拒絕，不合併
    """
    target = validate_space(space)
    row = conn.execute("SELECT space FROM vaults WHERE key = ?", (key,)).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (key,)
        ).fetchone()
        hint = f"（這是 {alias[0]!r} 的別名；請用正式 key）" if alias else ""
        raise UnknownVault(f"vault 不存在：{key!r}{hint}")
    source = row[0]
    if SPACE_DEV in (source, target):
        raise SpaceChangeRefused(
            f"不允許 {source} → {target}：dev 與 lore／personal 之間不互相轉換（A20）。"
            "dev 的 key 由 repo binding 決定、非 dev 以 '<space>/' 前綴命名，"
            "兩邊不共用 vault；換 space 只允許 lore↔personal"
        )
    if source == target:
        raise SpaceChangeRefused(f"vault {key!r} 已在 space {target!r}")
    if new_key is None:
        renamed = _swap_prefix(key, source, target, "key")
    else:
        renamed = canonical_key(new_key)
        check_key_prefix(target, renamed)
    taken = _name_taken(conn, renamed)
    if taken:
        raise VaultConflict(f"新 key 衝突：{taken}")
    aliases: list[tuple[str, str]] = []
    for (old_alias,) in conn.execute(
        "SELECT alias FROM vault_aliases WHERE vault = ? ORDER BY alias", (key,)
    ).fetchall():
        new_alias = _swap_prefix(old_alias, source, target, "別名")
        check_key_prefix(target, new_alias)
        if new_alias == renamed:
            raise VaultConflict(f"新別名 {new_alias!r} 與新 key 相同")
        taken = _name_taken(conn, new_alias)
        if taken:
            raise VaultConflict(f"新別名衝突：{taken}")
        aliases.append((old_alias, new_alias))

    columns = vault_reference_columns(conn)
    _check_detection(conn, columns)
    counts: dict[str, int] = {"vaults.key": 1, "vault_aliases.alias": len(aliases)}
    for table, column in columns:
        sql = f"SELECT count(*) FROM {_ident(table)} WHERE {_ident(column)} = ?"
        leftover = _count(conn, sql, (renamed,))
        if leftover:
            raise VaultConflict(
                f"{_ref_name(table, column)} 已有 {leftover} 列引用新 key {renamed!r}"
                "（殘留資料）；拒絕合併"
            )
        counts[_ref_name(table, column)] = _count(conn, sql, (key,))
    return SpaceChangePlan(
        key=key,
        new_key=renamed,
        from_space=source,
        to_space=target,
        aliases=tuple(aliases),
        counts=counts,
    )


def _rename_column(
    conn: sqlite3.Connection, table: str, column: str, old: str, new: str
) -> int:
    return conn.execute(
        f"UPDATE {_ident(table)} SET {_ident(column)} = ? WHERE {_ident(column)} = ?",
        (new, old),
    ).rowcount


def change_vault_space(
    conn: sqlite3.Connection, key: str, space: str, *, new_key: str | None = None
) -> SpaceChangePlan:
    """換 space 並改 key（單一交易）：vaults、別名與所有引用欄位一起改寫。

    交易內重新規劃再執行；執行後以資料實況逐欄核對「舊 key 零筆、新 key 筆數與
    規劃一致」並做外鍵檢查，任何不符整段 rollback（`PlanChanged`）。
    舊 key 不保留為別名。
    """
    with transaction(conn):
        # 外鍵沒有 ON UPDATE CASCADE：延到提交時檢查，才能先改 vaults.key 再改子表。
        # 提交前自行跑 foreign_key_check，不讓違規拖到 COMMIT 才爆。
        conn.execute("PRAGMA defer_foreign_keys = ON")
        plan = plan_space_change(conn, key, space, new_key=new_key)
        done: dict[str, int] = {
            "vaults.key": conn.execute(
                "UPDATE vaults SET key = ?, space = ? WHERE key = ?",
                (plan.new_key, plan.to_space, plan.key),
            ).rowcount
        }
        done["vault_aliases.alias"] = sum(
            conn.execute(
                "UPDATE vault_aliases SET alias = ? WHERE alias = ? AND vault = ?",
                (new_alias, old_alias, plan.key),
            ).rowcount
            for old_alias, new_alias in plan.aliases
        )
        for name in plan.counts:
            if name in done:
                continue
            table, column = name.split(".", 1)
            done[name] = _rename_column(conn, table, column, plan.key, plan.new_key)
        _verify_counts(plan.counts, done)
        _verify_renamed(conn, plan)
        return plan


def _verify_counts(expected: dict[str, int], done: dict[str, int]) -> None:
    diff = {
        name: (want, done.get(name))
        for name, want in expected.items()
        if done.get(name) != want
    }
    if diff:
        raise PlanChanged(f"實際改寫筆數與規劃不符：{diff}")


def _verify_renamed(conn: sqlite3.Connection, plan: SpaceChangePlan) -> None:
    """以資料實況核對（不信 rowcount）：舊 key 不再被引用、新 key 筆數與規劃一致。"""
    diff: dict[str, dict[str, int]] = {}
    for table, column in vault_reference_columns(conn):
        name = _ref_name(table, column)
        sql = f"SELECT count(*) FROM {_ident(table)} WHERE {_ident(column)} = ?"
        old_left = _count(conn, sql, (plan.key,))
        new_now = _count(conn, sql, (plan.new_key,))
        want = plan.counts.get(name, 0)
        if old_left or new_now != want:
            diff[name] = {"planned": want, "new_key": new_now, "old_key": old_left}
    row = conn.execute(
        "SELECT space FROM vaults WHERE key = ?", (plan.new_key,)
    ).fetchone()
    if row is None or row[0] != plan.to_space:
        diff["vaults.key"] = {"planned": 1, "new_key": 0 if row is None else 1}
    if conn.execute("SELECT 1 FROM vaults WHERE key = ?", (plan.key,)).fetchone():
        diff["vaults.key"] = {"planned": 1, "old_key": 1}
    for old_alias, new_alias in plan.aliases:
        owner = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (new_alias,)
        ).fetchone()
        stale = conn.execute(
            "SELECT 1 FROM vault_aliases WHERE alias = ?", (old_alias,)
        ).fetchone()
        if owner is None or owner[0] != plan.new_key or stale is not None:
            diff[f"vault_aliases.alias:{old_alias}"] = {"planned": 1}
    if diff:
        raise PlanChanged(f"改名後核對不符：{diff}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({r[0] for r in violations})
        raise PlanChanged(f"改名後外鍵不一致：{tables}")


# ── 墓碑清除 ──

PURGE_KIND_NOTE = "note"
PURGE_KIND_DOCUMENT = "document"
PURGE_KINDS = (PURGE_KIND_NOTE, PURGE_KIND_DOCUMENT)
_PURGE_BATCH = 500


@dataclass(frozen=True)
class TombstonePurgePlan:
    """將清除的墓碑（只有筆數、位元組與時間，不含標題與內文）。"""

    cutoff: str
    kinds: tuple[str, ...]
    note_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    # note 墓碑內容快照的總位元組（UTF-8）
    snapshot_bytes: int
    oldest: str | None
    newest: str | None
    # 會一併移除的匯入對帳清單列：(source, source_id, vault)
    manifest_rows: tuple[tuple[str, str, str], ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "note_tombstones": len(self.note_ids),
            "document_tombstones": len(self.document_ids),
            "import_manifest_rows": len(self.manifest_rows),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "cutoff": self.cutoff,
            "kinds": list(self.kinds),
            "counts": self.counts(),
            "snapshot_bytes": self.snapshot_bytes,
            "oldest_deleted_at": self.oldest,
            "newest_deleted_at": self.newest,
        }


def _purge_kinds(kinds: Sequence[str] | None) -> tuple[str, ...]:
    if kinds is None:
        return PURGE_KINDS
    if isinstance(kinds, str):
        raise TypeError("kinds 必須是清單，不可傳單一字串")
    wanted = tuple(dict.fromkeys(kinds))
    unknown = sorted(set(wanted) - set(PURGE_KINDS))
    if not wanted or unknown:
        raise ValueError(f"kinds 必須是 {list(PURGE_KINDS)} 的非空子集，得到 {kinds}")
    return wanted


def purge_cutoff(older_than_days: float, *, now: str | None = None) -> str:
    """刪除時間早於此字串（同 `utc_now` 格式，可直接比大小）的墓碑會被清除。"""
    if isinstance(older_than_days, bool) or not isinstance(
        older_than_days, int | float
    ):
        raise TypeError("older_than_days 必須是數字")
    if not older_than_days >= 0 or older_than_days == float("inf"):
        raise ValueError(f"older_than_days 必須是非負有限數，得到 {older_than_days}")
    moment = parse_utc(now if now is not None else utc_now())
    return format_utc(moment - timedelta(days=older_than_days))


def plan_tombstone_purge(
    conn: sqlite3.Connection,
    older_than_days: float,
    *,
    kinds: Sequence[str] | None = None,
    now: str | None = None,
) -> TombstonePurgePlan:
    """規劃（唯讀）：列出 `deleted_at` 早於 now − N 天的墓碑。"""
    wanted = _purge_kinds(kinds)
    cutoff = purge_cutoff(older_than_days, now=now)
    note_ids: list[str] = []
    doc_ids: list[str] = []
    stamps: list[str] = []
    snapshot_bytes = 0
    manifest: dict[tuple[str, str, str], None] = {}
    if PURGE_KIND_NOTE in wanted and _has_table(conn, "note_tombstones"):
        has_snapshot = _has_column(conn, "note_tombstones", "snapshot")
        size = "length(CAST(snapshot AS BLOB))" if has_snapshot else "0"
        rows = conn.execute(
            f"""
            SELECT note_id, deleted_at, coalesce({size}, 0), source, source_id
            FROM note_tombstones WHERE deleted_at < ? ORDER BY deleted_at, note_id
            """,
            (cutoff,),
        ).fetchall()
        has_manifest = _has_table(conn, "import_sources")
        for note_id, deleted_at, nbytes, source, source_id in rows:
            note_ids.append(note_id)
            stamps.append(deleted_at)
            snapshot_bytes += int(nbytes)
            if not has_manifest:
                continue
            for row in conn.execute(
                """
                SELECT source, source_id, vault FROM import_sources
                WHERE note_id = ? OR (source = ? AND source_id = ?)
                ORDER BY source, source_id
                """,
                (note_id, source, source_id),
            ):
                manifest[(row[0], row[1], row[2])] = None
    if PURGE_KIND_DOCUMENT in wanted and _has_table(conn, "document_tombstones"):
        for doc_id, deleted_at in conn.execute(
            """
            SELECT document_id, deleted_at FROM document_tombstones
            WHERE deleted_at < ? ORDER BY deleted_at, document_id
            """,
            (cutoff,),
        ):
            doc_ids.append(doc_id)
            stamps.append(deleted_at)
    return TombstonePurgePlan(
        cutoff=cutoff,
        kinds=wanted,
        note_ids=tuple(note_ids),
        document_ids=tuple(doc_ids),
        snapshot_bytes=snapshot_bytes,
        oldest=min(stamps) if stamps else None,
        newest=max(stamps) if stamps else None,
        manifest_rows=tuple(manifest),
    )


def _delete_ids(
    conn: sqlite3.Connection, table: str, column: str, ids: Sequence[str]
) -> int:
    deleted = 0
    for start in range(0, len(ids), _PURGE_BATCH):
        batch = tuple(ids[start : start + _PURGE_BATCH])
        placeholders = ",".join("?" * len(batch))
        deleted += conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", batch
        ).rowcount
    return deleted


def purge_tombstones(
    conn: sqlite3.Connection,
    older_than_days: float,
    *,
    kinds: Sequence[str] | None = None,
    now: str | None = None,
) -> tuple[TombstonePurgePlan, dict[str, int]]:
    """永久清除墓碑（單一交易內重新規劃再刪，核對筆數，不符整段 rollback）。

    note 墓碑連同內容快照一起刪；匯入過的 note 另移除其對帳清單列並把來源筆數減一
    （見模組說明）。回傳 (規劃, 實際刪除筆數)。
    """
    with transaction(conn):
        plan = plan_tombstone_purge(conn, older_than_days, kinds=kinds, now=now)
        done = {
            "note_tombstones": _delete_ids(
                conn, "note_tombstones", "note_id", plan.note_ids
            ),
            "document_tombstones": _delete_ids(
                conn, "document_tombstones", "document_id", plan.document_ids
            ),
            "import_manifest_rows": 0,
        }
        for source, source_id, vault in plan.manifest_rows:
            done["import_manifest_rows"] += conn.execute(
                "DELETE FROM import_sources WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).rowcount
            conn.execute(
                """
                UPDATE import_vault_counts SET source_count = source_count - 1
                WHERE source = ? AND vault = ? AND source_count > 0
                """,
                (source, vault),
            )
        if done != plan.counts():
            raise PlanChanged(
                f"實際清除筆數與規劃不符：規劃 {plan.counts()}、實際 {done}"
            )
    return plan, done
