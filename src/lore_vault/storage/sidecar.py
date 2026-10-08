"""側載小型機器狀態（schema v17 `sidecar_blobs`；D15 UI 已裁決 (a′)）。

通用、不檢索的 `(vault, key) → (mime, bytes, updated)`：

- 單列覆寫：put 即取代，不留版本、不留墓碑。資料的真相來源在客戶端，服務端只轉述
  最後一次收到的內容，遺失可由下一次推送重建
- **不進任何檢索或快照路徑**：不寫 FTS、不算 embedding、不進 recall／ask／list、
  不在 `GET /v1/snapshot` 白名單內。只有 `blob_put`／`blob_get` 讀寫這張表
- 內容上限 `MAX_BYTES`（64KB），超過直接拒絕、不截斷
- vault 刪除與換 space 由 `storage.admin` 在同一交易內一併處理；doctor
  `sidecar.orphans` 對帳參照完整性

本模組不認識任何使用者（例如任務層）；key 只是不透明的識別字。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .errors import NotFound
from .timeutil import utc_now
from .vaults import ALL_VAULTS, resolve_write, validate_space

TABLE = "sidecar_blobs"
MAX_BYTES = 64 * 1024
MAX_KEY_CHARS = 128
DEFAULT_MIME = "application/octet-stream"

# key：英數開頭，只含英數與 `. _ -`（不含任何路徑分隔字元）
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# mime：`type/subtype`（可帶參數），不收控制字元
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(;[ -~]*)?$")
_MAX_MIME_CHARS = 255


class InvalidSidecarKey(ValueError):
    """key 為空、過長，或含路徑分隔等不允許的字元（400 `invalid_key`）。"""


class SidecarTooLarge(ValueError):
    """內容超過 `MAX_BYTES`（413 `too_large`）。"""


@dataclass(frozen=True)
class SidecarBlob:
    vault: str
    space: str
    key: str
    mime: str
    content: bytes
    updated: str


def validate_key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise InvalidSidecarKey("key 必須是非空字串")
    if len(key) > MAX_KEY_CHARS:
        raise InvalidSidecarKey(f"key 不可超過 {MAX_KEY_CHARS} 字元")
    if not KEY_RE.match(key):
        raise InvalidSidecarKey(
            "key 只能用英數與 . _ -（英數開頭），不可含路徑分隔字元"
        )
    return key


def _validate_mime(mime: str | None) -> str:
    if mime is None:
        return DEFAULT_MIME
    if len(mime) > _MAX_MIME_CHARS or not _MIME_RE.match(mime):
        raise ValueError("mime 必須是 type/subtype 格式")
    return mime


def _row(row: sqlite3.Row) -> SidecarBlob:
    return SidecarBlob(
        vault=row["vault"],
        space=row["space"],
        key=row["key"],
        mime=row["mime"],
        content=bytes(row["content"]),
        updated=row["updated"],
    )


def put(
    conn: sqlite3.Connection,
    vault: str | None,
    key: str,
    content: bytes,
    *,
    space: object,
    mime: str | None = None,
) -> SidecarBlob:
    """以 (vault, key) 覆寫一份內容。vault 可為別名（存正式 key）；不存在或在別的
    space 拋 `UnknownVault`。"""
    checked_key = validate_key(key)
    checked_mime = _validate_mime(mime)
    if len(content) > MAX_BYTES:
        raise SidecarTooLarge(
            f"內容 {len(content)} 位元組，超過上限 {MAX_BYTES} 位元組"
        )
    updated = utc_now()
    with transaction(conn):
        vault_key = resolve_write(conn, vault, space=space)
        checked_space = validate_space(space)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (vault, space, key, mime, content, updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (vault, key) DO UPDATE SET
                space = excluded.space, mime = excluded.mime,
                content = excluded.content, updated = excluded.updated
            """,
            (vault_key, checked_space, checked_key, checked_mime, content, updated),
        )
    return SidecarBlob(
        vault_key, checked_space, checked_key, checked_mime, content, updated
    )


def get(
    conn: sqlite3.Connection, vault: str | None, key: str, *, space: object
) -> SidecarBlob:
    """單一 vault 的內容；不存在拋 `NotFound`。"""
    checked_key = validate_key(key)
    vault_key = resolve_write(conn, vault, space=space)
    row = conn.execute(
        f"""
        SELECT s.* FROM {TABLE} s JOIN vaults v ON v.key = s.vault
        WHERE s.vault = ? AND s.key = ? AND v.space = ? AND s.space = v.space
        """,
        (vault_key, checked_key, validate_space(space)),
    ).fetchone()
    if row is None:
        raise NotFound(f"vault {vault_key!r} 沒有 key {checked_key!r} 的側載內容")
    return _row(row)


def list_for_key(
    conn: sqlite3.Connection, key: str, *, space: object
) -> list[SidecarBlob]:
    """本 space 內所有存過該 key 的 vault（依 vault key 排序）；孤兒列不回傳。"""
    checked_key = validate_key(key)
    rows = conn.execute(
        f"""
        SELECT s.* FROM {TABLE} s JOIN vaults v ON v.key = s.vault
        WHERE s.key = ? AND s.space = ? AND v.space = s.space
        ORDER BY s.vault
        """,
        (checked_key, validate_space(space)),
    ).fetchall()
    return [_row(r) for r in rows]


def is_all(vault: str | None) -> bool:
    return vault is None or vault.strip() == ALL_VAULTS


# ── vault 刪除與換 space（storage.admin 在同一交易內呼叫）──


def has_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def count_for_vault(conn: sqlite3.Connection, vault: str) -> int:
    return int(
        conn.execute(
            f"SELECT count(*) FROM {TABLE} WHERE vault = ?", (vault,)
        ).fetchone()[0]
    )


def delete_for_vault(conn: sqlite3.Connection, vault: str) -> int:
    return conn.execute(f"DELETE FROM {TABLE} WHERE vault = ?", (vault,)).rowcount


def set_space_for_vault(conn: sqlite3.Connection, vault: str, space: str) -> int:
    return conn.execute(
        f"UPDATE {TABLE} SET space = ? WHERE vault = ?", (space, vault)
    ).rowcount


# ── doctor 對帳 ──


def orphans(conn: sqlite3.Connection) -> Reconciliation:
    """每列 `vault` 必須指向現存 vault，且 `space` 與該 vault 相符。

    兩種孤兒：vault 已刪除（刪除時沒一併清除）、vault 換了 space（沒一併改寫）。
    表沒有外鍵，只能靠寫入路徑與這裡對帳。"""
    total = int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
    dangling = conn.execute(
        f"""
        SELECT vault, key FROM {TABLE}
        WHERE vault NOT IN (SELECT key FROM vaults) ORDER BY vault, key
        """
    ).fetchall()
    mismatch = conn.execute(
        f"""
        SELECT s.vault, s.key, s.space, v.space FROM {TABLE} s
        JOIN vaults v ON v.key = s.vault
        WHERE s.space != v.space ORDER BY s.vault, s.key
        """
    ).fetchall()
    counts = {
        "rows": total,
        "dangling": len(dangling),
        "space_mismatch": len(mismatch),
    }
    if not dangling and not mismatch:
        return Reconciliation(
            "pass", f"{total} 列側載皆指向現存且 space 相符的 vault", counts
        )
    details = [f"{r[0]}／{r[1]}：vault 不存在" for r in dangling]
    details += [
        f"{r[0]}／{r[1]}：側載 space {r[2]!r} 與 vault 的 {r[3]!r} 不符"
        for r in mismatch
    ]
    return Reconciliation(
        "fail",
        f"{len(dangling) + len(mismatch)} 列側載成為孤兒（vault 已刪除或換了 space）",
        counts,
        tuple(details[:MAX_DETAILS]),
    )
