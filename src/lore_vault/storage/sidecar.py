"""側載小型機器狀態（schema v17 `sidecar_blobs`，v18 加版本；D15 UI 已裁決 (a′)、
D15 MCP 已裁決）。

通用、不檢索的 `(vault, key) → (mime, bytes, updated, version)`：

- 單列覆寫：put 即取代，不留舊內容、不留墓碑；每次 put `version` 遞增（新列從 1 起）
- 樂觀鎖（v18）：put 帶 `expected_version` 時在同一條 SQL 內比對版本，不符拋
  `SidecarVersionConflict`（附目前內容）、不寫入。`expected_version=0` 表示「預期尚不
  存在」：沒有列就建立，已有列即衝突。不帶 `expected_version` 照舊整份覆寫、不比對
- **不進任何檢索或快照路徑**：不寫 FTS、不算 embedding、不進 recall／ask／list、
  不在 `GET /v1/snapshot` 白名單內。只有 `blob_put`／`blob_get` 讀寫這張表
- 內容上限依 key 前綴（`limit_for_key`）：預設 `MAX_BYTES`（64KB），`task-` 開頭
  （任務層 change 全文、主 spec 鏡像）`LARGE_MAX_BYTES`（1MB）；超過直接拒絕、不截斷
- vault 刪除與換 space 由 `storage.admin` 在同一交易內一併處理；doctor
  `sidecar.orphans` 對帳參照完整性，`sidecar.version_conflict_integrity`
  自我驗證版本比對

本模組只依 key 前綴決定上限，不解讀 key 背後的語意；誰能寫哪些前綴由 API 層決定
（任務層遠端同步開關擋 `task-` 前綴，見 `api.routes`）。
"""

from __future__ import annotations

import base64
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from lore_vault.schema import SPACE_DEV, Vault

from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .errors import NotFound
from .migrate import migrate
from .timeutil import utc_now
from .vaults import ALL_VAULTS, resolve_write, upsert_vault, validate_space

TABLE = "sidecar_blobs"
MAX_BYTES = 64 * 1024
LARGE_MAX_BYTES = 1024 * 1024
# 任務層的權威內容（`task-change:<name>`、`task-spec-mirror:<capability>` 等，設計
# TASK_LAYER_MCP §1.2）。注意推導快照 `tasks-snapshot` 不屬此前綴
TASK_KEY_PREFIX = "task-"
# (前綴, 上限)；第一個符合的生效，都不符用 MAX_BYTES
_LIMITS_BY_PREFIX: tuple[tuple[str, int], ...] = ((TASK_KEY_PREFIX, LARGE_MAX_BYTES),)
MAX_KEY_CHARS = 128
DEFAULT_MIME = "application/octet-stream"

# key：英數開頭，只含英數與 `. _ - :`（不含任何路徑分隔字元；`:` 供任務層
# `task-change:<name>` 這類階層式 key 使用，key 不會拿去當檔名）
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# mime：`type/subtype`（可帶參數），不收控制字元
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(;[ -~]*)?$")
_MAX_MIME_CHARS = 255


class InvalidSidecarKey(ValueError):
    """key 為空、過長，或含路徑分隔等不允許的字元（400 `invalid_key`）。"""


class SidecarTooLarge(ValueError):
    """內容超過該 key 的上限（`limit_for_key`；413 `too_large`）。"""


@dataclass(frozen=True)
class SidecarBlob:
    vault: str
    space: str
    key: str
    mime: str
    content: bytes
    updated: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        """HTTP 回應形狀（`blob_get` 與 409 `version_conflict` 的 `current` 共用）。"""
        return {
            "vault": self.vault,
            "key": self.key,
            "mime": self.mime,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
            "updated": self.updated,
            "version": self.version,
        }


class SidecarVersionConflict(Exception):
    """`expected_version` 與目前版本不符，未寫入（409 `version_conflict`）。

    `current` 為目前內容；`expected_version > 0` 但 key 尚不存在時為 None。
    刻意不繼承 ValueError（否則會被 400 `invalid_request` 吃掉）。"""

    def __init__(self, expected: int, current: SidecarBlob | None, key: str) -> None:
        self.expected = expected
        self.current = current
        actual = "不存在" if current is None else str(current.version)
        super().__init__(f"側載 {key!r} 版本衝突：預期 {expected}，目前 {actual}")


def limit_for_key(key: str) -> int:
    """該 key 的內容上限（位元組）。"""
    for prefix, limit in _LIMITS_BY_PREFIX:
        if key.startswith(prefix):
            return limit
    return MAX_BYTES


def is_task_key(key: str) -> bool:
    """任務層權威內容的 key（受任務層遠端同步開關管制）。"""
    return key.startswith(TASK_KEY_PREFIX)


def validate_key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise InvalidSidecarKey("key 必須是非空字串")
    if len(key) > MAX_KEY_CHARS:
        raise InvalidSidecarKey(f"key 不可超過 {MAX_KEY_CHARS} 字元")
    if not KEY_RE.match(key):
        raise InvalidSidecarKey(
            "key 只能用英數與 . _ - :（英數開頭），不可含路徑分隔字元"
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
        version=int(row["version"]),
    )


def put(
    conn: sqlite3.Connection,
    vault: str | None,
    key: str,
    content: bytes,
    *,
    space: object,
    mime: str | None = None,
    expected_version: int | None = None,
) -> SidecarBlob:
    """以 (vault, key) 覆寫一份內容，版本遞增。vault 可為別名（存正式 key）；不存在或
    在別的 space 拋 `UnknownVault`。

    `expected_version`：None 不比對；0 表示預期尚不存在；正整數須等於目前版本。
    不符拋 `SidecarVersionConflict`（附目前內容），不寫入。"""
    checked_key = validate_key(key)
    checked_mime = _validate_mime(mime)
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError("expected_version 必須是非負整數")
    limit = limit_for_key(checked_key)
    if len(content) > limit:
        raise SidecarTooLarge(f"內容 {len(content)} 位元組，超過上限 {limit} 位元組")
    updated = utc_now()
    with transaction(conn):
        vault_key = resolve_write(conn, vault, space=space)
        checked_space = validate_space(space)
        row = conn.execute(
            _put_sql(expected_version),
            {
                "vault": vault_key,
                "space": checked_space,
                "key": checked_key,
                "mime": checked_mime,
                "content": content,
                "updated": updated,
                "expected": expected_version,
            },
        ).fetchone()
        if row is None:
            raise SidecarVersionConflict(
                expected_version or 0,
                _current(conn, vault_key, checked_key),
                checked_key,
            )
        version = int(row[0])
    return SidecarBlob(
        vault_key, checked_space, checked_key, checked_mime, content, updated, version
    )


_INSERT = f"""
    INSERT INTO {TABLE} (vault, space, key, mime, content, updated)
    VALUES (:vault, :space, :key, :mime, :content, :updated)
"""


def _put_sql(expected_version: int | None) -> str:
    """回傳寫入並 `RETURNING version` 的 SQL；沒回列＝版本不符（未寫入）。

    版本比對與寫入在同一條敘述內完成（且在 BEGIN IMMEDIATE 內），不先讀後寫。"""
    if expected_version is None:
        return f"""{_INSERT}
            ON CONFLICT (vault, key) DO UPDATE SET
                space = excluded.space, mime = excluded.mime,
                content = excluded.content, updated = excluded.updated,
                version = version + 1
            RETURNING version"""
    if expected_version == 0:
        # 預期尚不存在：已有列就不動（DO NOTHING 不回列）
        return f"""{_INSERT}
            ON CONFLICT (vault, key) DO NOTHING
            RETURNING version"""
    return f"""
        UPDATE {TABLE} SET
            space = :space, mime = :mime, content = :content, updated = :updated,
            version = version + 1
        WHERE vault = :vault AND key = :key AND version = :expected
        RETURNING version"""


def _current(conn: sqlite3.Connection, vault_key: str, key: str) -> SidecarBlob | None:
    row = conn.execute(
        f"SELECT * FROM {TABLE} WHERE vault = ? AND key = ?", (vault_key, key)
    ).fetchone()
    return None if row is None else _row(row)


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


def has_version_column(conn: sqlite3.Connection) -> bool:
    """v18 以後才有 `version` 欄。"""
    return any(r[1] == "version" for r in conn.execute(f"PRAGMA table_info({TABLE})"))


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


_PROBE_VAULT = "folder/sidecar-version-probe"
_PROBE_KEY = "task-probe:version"


def _probe_version_lock() -> list[str]:
    """在獨立的記憶體資料庫跑一次真的 `put`，確認版本比對有在擋。回傳問題清單。

    不碰正式資料庫（doctor 不寫入）；走的是與 `blob_put` 相同的 `put` 程式碼。"""
    problems: list[str] = []
    probe = sqlite3.connect(":memory:", isolation_level=None)
    probe.row_factory = sqlite3.Row
    try:
        migrate(probe)
        upsert_vault(
            probe,
            Vault(key=_PROBE_VAULT, display="probe", kind="repo", space=SPACE_DEV),
        )
        first = put(probe, _PROBE_VAULT, _PROBE_KEY, b"v1", space=SPACE_DEV)
        second = put(
            probe,
            _PROBE_VAULT,
            _PROBE_KEY,
            b"v2",
            space=SPACE_DEV,
            expected_version=first.version,
        )
        if second.version != first.version + 1:
            problems.append(
                f"帶正確 expected_version 寫入後版本為 {second.version}，"
                f"應為 {first.version + 1}"
            )
        for stale, label in ((first.version, "過期版本"), (0, "expected_version=0")):
            try:
                put(
                    probe,
                    _PROBE_VAULT,
                    _PROBE_KEY,
                    b"stale",
                    space=SPACE_DEV,
                    expected_version=stale,
                )
            except SidecarVersionConflict as exc:
                if exc.current is None or exc.current.content != b"v2":
                    problems.append(f"{label}衝突時未附目前內容")
            else:
                problems.append(f"帶{label}的 put 沒有被拒（默默覆寫）")
        stored = get(probe, _PROBE_VAULT, _PROBE_KEY, space=SPACE_DEV)
        if stored.content != b"v2" or stored.version != second.version:
            problems.append(
                f"衝突後內容或版本被改動（版本 {stored.version}，"
                f"應為 {second.version}）"
            )
    finally:
        probe.close()
    return problems


def version_conflict_integrity(conn: sqlite3.Connection) -> Reconciliation:
    """`blob_put` 帶過期 `expected_version` 必須被拒、附目前內容、不覆寫（v18）。

    兩部分：正式庫的每列版本皆 >= 1（遷移與寫入路徑都有維持）；以及在記憶體資料庫
    實際跑一次版本衝突，證明比對程式碼有在擋（拿掉比對時這裡會 fail）。"""
    total = int(conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
    bad = conn.execute(
        f"""
        SELECT vault, key, version FROM {TABLE}
        WHERE typeof(version) != 'integer' OR version < 1 ORDER BY vault, key
        """
    ).fetchall()
    problems = [f"{r[0]}／{r[1]}：版本 {r[2]!r} 不是正整數" for r in bad]
    probe_problems = _probe_version_lock()
    problems += [f"自我驗證：{p}" for p in probe_problems]
    counts = {
        "rows": total,
        "invalid_version": len(bad),
        "probe_failures": len(probe_problems),
    }
    if not problems:
        return Reconciliation(
            "pass", f"{total} 列側載版本有效；過期版本的寫入會被拒並附目前內容", counts
        )
    return Reconciliation(
        "fail",
        f"側載版本鎖有 {len(problems)} 個問題（可能默默覆寫他人的修改）",
        counts,
        tuple(problems[:MAX_DETAILS]),
    )
