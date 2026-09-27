"""執行期設定覆寫的儲存與稽核（D13，schema v15）。純標準庫。

- `settings_overrides`：鍵 → JSON 值；白名單與值驗證在 `lore_vault.runtime_settings`
- `settings_audit`：每次修改／還原一列：時間、鍵、動作、生效值舊→新、principal、顯示名稱
- `change()` 在單一交易內驗證整批、寫覆寫與稽核：
  任一鍵不合法整批不寫（`InvalidSettings`）
- 讀取端（`effective_overrides`）遇到不合法的覆寫列（手動改 DB、白名單移除的鍵）一律
  略過、改用預設值，不讓服務因此起不來；doctor `settings.overrides` 會把它標成 fail
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lore_vault.config import Config, ConfigError
from lore_vault.runtime_settings import (
    InvalidSetting,
    apply_overrides,
    base_value,
    spec_for,
    validate_value,
)

from .checks import MAX_DETAILS, Reconciliation
from .db import transaction

ACTION_SET = "set"
ACTION_RESET = "reset"
DEFAULT_AUDIT_LIMIT = 20
MAX_AUDIT_LIMIT = 200


class MissingSettingsTables(LookupError):
    """資料庫尚未遷移到含執行期設定的版本（v15）。"""


class InvalidSettings(ValueError):
    """整批修改有不合法的鍵或值；`errors` 為逐鍵的 `InvalidSetting`。"""

    def __init__(self, errors: Sequence[InvalidSetting]) -> None:
        super().__init__("；".join(f"{e.key}：{e}" for e in errors))
        self.errors = tuple(errors)


@dataclass(frozen=True)
class OverrideRow:
    key: str
    raw: str
    updated: str
    updated_by: str


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str
    key: str
    action: str
    old_value: Any
    new_value: Any
    principal: str
    display: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "key": self.key,
            "action": self.action,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "principal": self.principal,
            "display": self.display,
        }


def has_tables(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('settings_overrides', 'settings_audit')"
        ).fetchone()[0]
        == 2
    )


def _require_tables(conn: sqlite3.Connection) -> None:
    if not has_tables(conn):
        raise MissingSettingsTables("缺少執行期設定表（schema 未遷移到 v15）")


def override_rows(conn: sqlite3.Connection) -> list[OverrideRow]:
    _require_tables(conn)
    return [
        OverrideRow(r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT key, value, updated, updated_by FROM settings_overrides "
            "ORDER BY key"
        )
    ]


def _check_row(row: OverrideRow) -> Any:
    """一列覆寫的有效值；不合法拋 `InvalidSetting`。"""
    try:
        value = json.loads(row.raw)
    except ValueError:
        raise InvalidSetting(row.key, "invalid_value", "不是合法 JSON") from None
    return validate_value(row.key, value)


def effective_overrides(
    conn: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, OverrideRow], list[tuple[OverrideRow, str]]]:
    """(合法的覆寫值, 合法覆寫的列, [(不合法的列, 原因)])。表不存在時全空。"""
    if not has_tables(conn):
        return {}, {}, []
    values: dict[str, Any] = {}
    rows: dict[str, OverrideRow] = {}
    invalid: list[tuple[OverrideRow, str]] = []
    for row in override_rows(conn):
        try:
            values[row.key] = _check_row(row)
        except InvalidSetting as exc:
            invalid.append((row, str(exc)))
            continue
        rows[row.key] = row
    return values, rows, invalid


def read_override(conn: sqlite3.Connection, key: str) -> Any | None:
    """單一鍵的合法覆寫值；沒有覆寫、表不存在或值不合法回 None。"""
    values, _, _ = effective_overrides(conn)
    return values.get(key)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def change(
    conn: sqlite3.Connection,
    base: Config,
    *,
    set_values: Mapping[str, Any] | None = None,
    reset_keys: Sequence[str] = (),
    principal: str,
    display: str | None,
    now: str,
) -> list[AuditEntry]:
    """修改與還原（同一交易）。回傳這次寫入的稽核列；值沒有變動的鍵不寫、不記稽核。

    - `set_values`：鍵 → 新值（覆寫）；與目前覆寫相同時略過
    - `reset_keys`：刪除覆寫、回到設定檔／環境變數的預設值；原本就沒有覆寫時略過
    - 同一鍵不可同時修改與還原；任一鍵不合法、或合起來違反設定規則 → `InvalidSettings`
    """
    set_values = dict(set_values or {})
    errors: list[InvalidSetting] = []
    cleaned: dict[str, Any] = {}
    for key, value in set_values.items():
        try:
            cleaned[key] = validate_value(key, value)
        except InvalidSetting as exc:
            errors.append(exc)
    for key in reset_keys:
        try:
            spec_for(key)
        except InvalidSetting as exc:
            errors.append(exc)
        if key in set_values:
            errors.append(
                InvalidSetting(key, "invalid_value", "同一個設定不可同時修改與還原")
            )
    if errors:
        raise InvalidSettings(errors)

    entries: list[AuditEntry] = []
    with transaction(conn):
        _require_tables(conn)
        current, _, _ = effective_overrides(conn)
        existing = {r.key for r in override_rows(conn)}
        after = {k: v for k, v in current.items() if k not in reset_keys}
        after.update(cleaned)
        try:
            apply_overrides(base, after)
        except ConfigError as exc:
            raise InvalidSettings(
                [InvalidSetting(k, "invalid_value", str(exc)) for k in cleaned]
            ) from None
        for key, value in cleaned.items():
            if key in current and current[key] == value:
                continue
            old = current.get(key, base_value(base, key))
            conn.execute(
                """
                INSERT INTO settings_overrides (key, value, updated, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value, updated = excluded.updated,
                    updated_by = excluded.updated_by
                """,
                (key, _dump(value), now, principal),
            )
            entries.append(
                _audit(conn, now, key, ACTION_SET, old, value, principal, display)
            )
        for key in reset_keys:
            if key not in existing:
                continue
            old = current.get(key, base_value(base, key))
            conn.execute("DELETE FROM settings_overrides WHERE key = ?", (key,))
            entries.append(
                _audit(
                    conn,
                    now,
                    key,
                    ACTION_RESET,
                    old,
                    base_value(base, key),
                    principal,
                    display,
                )
            )
    return entries


def _audit(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    action: str,
    old: Any,
    new: Any,
    principal: str,
    display: str | None,
) -> AuditEntry:
    cursor = conn.execute(
        """
        INSERT INTO settings_audit (at, key, action, old_value, new_value,
                                    principal, display)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (now, key, action, _dump(old), _dump(new), principal, display),
    )
    seq = cursor.lastrowid
    assert seq is not None
    return AuditEntry(seq, now, key, action, old, new, principal, display)


def audit_log(
    conn: sqlite3.Connection, *, limit: int = DEFAULT_AUDIT_LIMIT
) -> list[AuditEntry]:
    """最近的稽核紀錄（新到舊）。"""
    _require_tables(conn)
    if not 1 <= limit <= MAX_AUDIT_LIMIT:
        raise ValueError(f"limit 必須介於 1 與 {MAX_AUDIT_LIMIT}")
    rows = conn.execute(
        """
        SELECT seq, at, key, action, old_value, new_value, principal, display
        FROM settings_audit ORDER BY seq DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        AuditEntry(
            r[0], r[1], r[2], r[3], json.loads(r[4]), json.loads(r[5]), r[6], r[7]
        )
        for r in rows
    ]


# ── 對帳（doctor 在 builtin.py 轉成 CheckResult）────────────────────────


def overrides_validity(conn: sqlite3.Connection) -> Reconciliation:
    """每一列覆寫都在白名單內、型別與範圍合法，合起來也符合設定規則。

    非零為 fail：不合法的列在執行期被略過（改用預設值），UI 上改的設定其實沒生效。
    """
    _require_tables(conn)
    values, _, invalid = effective_overrides(conn)
    details = [f"{row.key}：{reason}" for row, reason in invalid]
    try:
        apply_overrides(Config(), values)
    except (ConfigError, InvalidSetting) as exc:
        details.append(f"覆寫值合起來違反設定規則：{exc}")
    counts = {"overrides": len(values) + len(invalid), "invalid": len(details)}
    if details:
        return Reconciliation(
            "fail",
            f"{len(details)} 項設定覆寫不合法（執行期被略過，未生效）",
            counts,
            tuple(details[:MAX_DETAILS]),
        )
    return Reconciliation("pass", f"{len(values)} 項設定覆寫，皆合法", counts)


def audit_agreement(conn: sqlite3.Connection) -> Reconciliation:
    """覆寫表與稽核一致：有覆寫的鍵，最後一筆稽核是 set 且新值等於覆寫值；
    沒有覆寫但有稽核的鍵，最後一筆是 reset。

    不一致代表有繞過 `change()` 的寫入（沒留下誰改的紀錄），為 fail。
    """
    _require_tables(conn)
    last = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            """
            SELECT a.key, a.action, a.new_value FROM settings_audit a
            JOIN (SELECT key, max(seq) AS seq FROM settings_audit GROUP BY key) m
              ON m.seq = a.seq
            """
        )
    }
    details: list[str] = []
    rows = override_rows(conn)
    for row in rows:
        entry = last.get(row.key)
        if entry is None:
            details.append(f"{row.key}：有覆寫但沒有任何稽核紀錄")
            continue
        action, new_value = entry
        if action != ACTION_SET or not _same_json(new_value, row.raw):
            details.append(f"{row.key}：覆寫值與最後一筆稽核不符")
    overridden = {r.key for r in rows}
    for key, (action, _) in last.items():
        if key not in overridden and action != ACTION_RESET:
            details.append(f"{key}：稽核記錄為修改，覆寫卻已不在")
    counts = {
        "overrides": len(rows),
        "audited_keys": len(last),
        "mismatch": len(details),
    }
    if details:
        return Reconciliation(
            "fail",
            f"{len(details)} 項設定與稽核紀錄不一致（有未經稽核的寫入）",
            counts,
            tuple(details[:MAX_DETAILS]),
        )
    return Reconciliation("pass", "設定覆寫與稽核紀錄一致", counts)


def _same_json(a: str, b: str) -> bool:
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return False
