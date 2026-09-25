"""vault 硬過濾（T-16）與 vault／別名的讀寫。

所有讀寫函式的 vault 參數都經過這裡：
- 未傳、非字串、空字串 → `VaultRequired`（不會變成全域查詢）
- `"*"` 只在讀取時代表跨 vault，寫入時拒絕
- key 一律 `canonical_key`（小寫）；別名解析到現行 key（A7：改名在讀取端接起來）
- 不存在的 key → `UnknownVault`（不回空結果、不自動建立）

查詢的 vault 條件統一由 `vault_clause` 產生，這是過濾的唯一出口。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lore_vault.schema import Vault, canonical_key

from .db import transaction
from .errors import UnknownVault, VaultConflict, VaultRequired
from .timeutil import utc_now

ALL_VAULTS = "*"


@dataclass(frozen=True)
class VaultScope:
    """解析後的讀取範圍：`key` 為 None 代表明示的跨 vault（`"*"`）。"""

    key: str | None

    @property
    def is_all(self) -> bool:
        return self.key is None


def _validate_raw(vault: object) -> str:
    if vault is None:
        raise VaultRequired("必須指定 vault；跨 vault 查詢請明示 vault='*'")
    if not isinstance(vault, str):
        raise VaultRequired(f"vault 必須是字串，得到 {type(vault).__name__}")
    if not vault.strip():
        raise VaultRequired("vault 不可為空字串；跨 vault 查詢請明示 vault='*'")
    if vault != vault.strip():
        raise VaultRequired(f"vault 前後不可有空白：{vault!r}")
    return vault


def _lookup(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT key FROM vaults WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute(
        "SELECT vault FROM vault_aliases WHERE alias = ?", (key,)
    ).fetchone()
    if row is not None:
        return row[0]
    raise UnknownVault(f"vault 不存在：{key!r}")


def resolve_read(conn: sqlite3.Connection, vault: object) -> VaultScope:
    raw = _validate_raw(vault)
    if raw == ALL_VAULTS:
        return VaultScope(None)
    return VaultScope(_lookup(conn, canonical_key(raw)))


def resolve_write(conn: sqlite3.Connection, vault: object) -> str:
    raw = _validate_raw(vault)
    if raw == ALL_VAULTS:
        raise VaultRequired("寫入必須指定單一 vault，不可用 '*'")
    return _lookup(conn, canonical_key(raw))


def vault_clause(scope: VaultScope, column: str) -> tuple[str, tuple[str, ...]]:
    """產生 vault 過濾條件。所有讀取查詢都必須經過這裡。"""
    if scope.is_all:
        return "1 = 1", ()
    return f"{column} = ?", (scope.key,)  # type: ignore[return-value]


# ── vault 本身的讀寫 ────────────────────────────────────────────────


ORIGIN_MANUAL = "manual"
ORIGIN_EPISODE = "episode"
ORIGIN_PIPELINE = "pipeline"
VAULT_ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_EPISODE, ORIGIN_PIPELINE})
AUTO_ORIGINS = frozenset({ORIGIN_EPISODE, ORIGIN_PIPELINE})


def upsert_vault(
    conn: sqlite3.Connection,
    vault: Vault,
    *,
    origin: str = ORIGIN_MANUAL,
    origin_detail: str | None = None,
) -> None:
    """新增或更新 vault 與其別名（別名以傳入的為準，整批替換）。

    別名不可撞到其他 vault 的 key 或別名；key 也不可撞到其他 vault 的別名。
    `origin`／`origin_detail` 只在新建時寫入；更新既有 vault 不改來源標記。
    """
    if origin not in VAULT_ORIGINS:
        raise ValueError(f"origin 必須是 {sorted(VAULT_ORIGINS)}，得到 {origin!r}")
    with transaction(conn):
        owner = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (vault.key,)
        ).fetchone()
        if owner is not None and owner[0] != vault.key:
            raise VaultConflict(f"key {vault.key!r} 已是 vault {owner[0]!r} 的別名")
        for alias in vault.aliases:
            if conn.execute("SELECT 1 FROM vaults WHERE key = ?", (alias,)).fetchone():
                raise VaultConflict(f"別名 {alias!r} 已是另一個 vault 的 key")
            owner = conn.execute(
                "SELECT vault FROM vault_aliases WHERE alias = ?", (alias,)
            ).fetchone()
            if owner is not None and owner[0] != vault.key:
                raise VaultConflict(f"別名 {alias!r} 已屬於 vault {owner[0]!r}")
        conn.execute(
            """
            INSERT INTO vaults (key, display, kind, created, origin, origin_detail)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET display = excluded.display,
                                            kind = excluded.kind
            """,
            (vault.key, vault.display, vault.kind, utc_now(), origin, origin_detail),
        )
        conn.execute("DELETE FROM vault_aliases WHERE vault = ?", (vault.key,))
        conn.executemany(
            "INSERT INTO vault_aliases (alias, vault) VALUES (?, ?)",
            [(alias, vault.key) for alias in vault.aliases],
        )


def ensure_vault(
    conn: sqlite3.Connection,
    vault: Vault,
    *,
    origin: str,
    origin_detail: str | None = None,
) -> tuple[str, bool]:
    """vault（key 或別名）已存在就回傳現行 key；不存在才以 `origin` 建立。

    回傳 (現行 key, 是否新建)。只給明確允許自動建立的路徑用（episode 收料、
    管線的 `global`）；notes 的 write 仍然不自動建（拼錯字不可產生幽靈範圍）。
    """
    if origin not in AUTO_ORIGINS:
        raise ValueError(f"自動建立的 origin 必須是 {sorted(AUTO_ORIGINS)}")
    with transaction(conn):
        try:
            return resolve_write(conn, vault.key), False
        except UnknownVault:
            pass
        upsert_vault(conn, vault, origin=origin, origin_detail=origin_detail)
        return vault.key, True


def vault_origins(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """所有 vault 的 (key, origin, origin_detail)，依 key 排序（doctor 用）。"""
    return [
        (r["key"], r["origin"], r["origin_detail"])
        for r in conn.execute(
            "SELECT key, origin, origin_detail FROM vaults ORDER BY key"
        )
    ]


def _row_to_vault(conn: sqlite3.Connection, row: sqlite3.Row) -> Vault:
    aliases = [
        r[0]
        for r in conn.execute(
            "SELECT alias FROM vault_aliases WHERE vault = ? ORDER BY alias",
            (row["key"],),
        )
    ]
    return Vault(
        key=row["key"], display=row["display"], kind=row["kind"], aliases=aliases
    )


def get_vault(conn: sqlite3.Connection, vault: str) -> Vault:
    """以 key 或別名取 vault；不存在拋 `UnknownVault`。"""
    key = resolve_write(conn, vault)
    row = conn.execute("SELECT * FROM vaults WHERE key = ?", (key,)).fetchone()
    return _row_to_vault(conn, row)


def list_vaults(conn: sqlite3.Connection) -> list[Vault]:
    rows = conn.execute("SELECT * FROM vaults ORDER BY key").fetchall()
    return [_row_to_vault(conn, row) for row in rows]
