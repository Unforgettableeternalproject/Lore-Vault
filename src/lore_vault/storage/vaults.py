"""vault 硬過濾（T-16）、space 硬過濾（A18，T-53）與 vault／別名的讀寫。

所有讀寫函式的 vault 參數都經過這裡：
- 未傳、非字串、空字串 → `VaultRequired`（不會變成全域查詢）
- `"*"` 只在讀取時代表跨 vault，寫入時拒絕
- key 一律 `canonical_key`（小寫）；別名解析到現行 key（A7：改名在讀取端接起來）
- 不存在的 key → `UnknownVault`（不回空結果、不自動建立）

space 與 vault 是 AND 疊加的兩層範圍：
- `space` 必填、無預設（未傳 → `SpaceRequired`；不在白名單 → `InvalidSpace`）
- key（或別名）存在但屬於別的 space → 一樣是 `UnknownVault`，不透露存在性
- `"*"` 只解除 vault 這一層：代表「該 space 內的全部 vault」

查詢的範圍條件統一由 `vault_clause` 產生，這是過濾的唯一出口。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lore_vault.schema import SPACE_DEV, SPACES, Vault, canonical_key

from .db import transaction
from .errors import (
    InvalidSpace,
    ReservedVault,
    SpaceKeyPrefixRequired,
    SpaceRequired,
    UnknownVault,
    VaultConflict,
    VaultRequired,
)
from .timeutil import utc_now

ALL_VAULTS = "*"


@dataclass(frozen=True)
class VaultScope:
    """解析後的讀取範圍：`key` 為 None 代表明示的跨 vault（`"*"`），
    仍限定在 `space` 內。"""

    key: str | None
    space: str

    @property
    def is_all(self) -> bool:
        return self.key is None


def validate_space(space: object) -> str:
    """space 參數驗證。

    未傳／非字串／空字串 → `SpaceRequired`；不在白名單 → `InvalidSpace`。
    """
    if space is None:
        raise SpaceRequired(f"必須指定 space（{sorted(SPACES)} 之一）；服務端沒有預設")
    if not isinstance(space, str):
        raise SpaceRequired(f"space 必須是字串，得到 {type(space).__name__}")
    if not space.strip():
        raise SpaceRequired("space 不可為空字串")
    if space not in SPACES:
        raise InvalidSpace(f"space 必須是 {sorted(SPACES)} 之一，得到 {space!r}")
    return space


def check_key_prefix(space: str, key: str) -> None:
    """非 dev space 的 key 必須以 `<space>/` 開頭。

    dev 沿用 binding 算出的 key，不檢查。
    """
    space = validate_space(space)
    if space == SPACE_DEV:
        return
    key = canonical_key(key)
    if not key.startswith(f"{space}/") or key == f"{space}/":
        raise SpaceKeyPrefixRequired(
            f"space {space!r} 的 vault key 必須以 '{space}/' 開頭，得到 {key!r}"
        )


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


def _lookup(conn: sqlite3.Connection, key: str, space: str) -> str:
    row = conn.execute(
        "SELECT key FROM vaults WHERE key = ? AND space = ?", (key, space)
    ).fetchone()
    if row is not None:
        return row[0]
    # 別名也要過 space：否則別名會成為跨 space 存在性洩漏的側門
    row = conn.execute(
        """
        SELECT a.vault FROM vault_aliases a JOIN vaults v ON v.key = a.vault
        WHERE a.alias = ? AND v.space = ?
        """,
        (key, space),
    ).fetchone()
    if row is not None:
        return row[0]
    raise UnknownVault(f"space {space!r} 內 vault 不存在：{key!r}")


def resolve_read(
    conn: sqlite3.Connection, vault: object, *, space: object
) -> VaultScope:
    raw = _validate_raw(vault)
    checked = validate_space(space)
    if raw == ALL_VAULTS:
        return VaultScope(None, checked)
    return VaultScope(_lookup(conn, canonical_key(raw), checked), checked)


def resolve_write(conn: sqlite3.Connection, vault: object, *, space: object) -> str:
    raw = _validate_raw(vault)
    checked = validate_space(space)
    if raw == ALL_VAULTS:
        raise VaultRequired("寫入必須指定單一 vault，不可用 '*'")
    return _lookup(conn, canonical_key(raw), checked)


def vault_clause(scope: VaultScope, column: str) -> tuple[str, tuple[str, ...]]:
    """產生 vault＋space 過濾條件。所有讀取查詢都必須經過這裡。

    單一 vault：key 已在 `resolve_read` 限定於 space 內解析；
    `"*"`：限定為該 space 內的全部 vault（不是全資料庫）。
    """
    if scope.is_all:
        return f"{column} IN (SELECT key FROM vaults WHERE space = ?)", (scope.space,)
    return f"{column} = ?", (scope.key,)  # type: ignore[return-value]


# ── vault 本身的讀寫 ────────────────────────────────────────────────


ORIGIN_MANUAL = "manual"
ORIGIN_EPISODE = "episode"
ORIGIN_PIPELINE = "pipeline"
VAULT_ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_EPISODE, ORIGIN_PIPELINE})
AUTO_ORIGINS = frozenset({ORIGIN_EPISODE, ORIGIN_PIPELINE})

# 雜項 vault（D14）：收容從尚未建立 vault 的位置（binding key `folder/<名稱>`）擷取的
# episode，與跨專案知識的 `global` 不同。key 不含 `/`，不會與 binding 算出的 key
# （`host/owner/repo`、`folder/<名稱>`）或非 dev 的 `<space>/` 前綴相撞。
# 規則（`check_reserved`）：key `misc` ⇔ kind `misc`、不可有別名、也不可被當成別名——
# 否則 `vault_resolve` 可能把一般專案解析到雜項。只由 episode 收料路徑自動建立。
KIND_MISC = "misc"
MISC_VAULT_KEY = "misc"
MISC_VAULT_DISPLAY = "雜項"
# 會被改路由到雜項 vault 的 binding key 前綴（沒有 git remote 的位置）
FOLDER_KEY_PREFIX = "folder/"


def check_reserved(vault: Vault) -> None:
    """雜項 vault 的保留規則；違反拋 `ReservedVault`。"""
    if (vault.key == MISC_VAULT_KEY) != (vault.kind == KIND_MISC):
        raise ReservedVault(
            f"key {MISC_VAULT_KEY!r} 保留給雜項 vault（kind={KIND_MISC!r}），"
            "兩者必須同時成立；雜項 vault 只由 episode 收料自動建立"
        )
    if vault.kind == KIND_MISC and vault.aliases:
        raise ReservedVault("雜項 vault 不可有別名")
    if MISC_VAULT_KEY in vault.aliases:
        raise ReservedVault(f"{MISC_VAULT_KEY!r} 保留給雜項 vault，不可當別名")


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
    key 與別名都要符合 `vault.space` 的前綴規則（`check_key_prefix`）；
    既有 vault 的 space 不在這裡改（換 space 只走管理指令，連 key 一起改：
    `storage.admin.change_vault_space`，A20）。
    """
    if origin not in VAULT_ORIGINS:
        raise ValueError(f"origin 必須是 {sorted(VAULT_ORIGINS)}，得到 {origin!r}")
    check_reserved(vault)
    for name in (vault.key, *vault.aliases):
        check_key_prefix(vault.space, name)
    with transaction(conn):
        current = conn.execute(
            "SELECT space FROM vaults WHERE key = ?", (vault.key,)
        ).fetchone()
        if current is not None and current[0] != vault.space:
            raise VaultConflict(
                f"vault {vault.key!r} 已存在於另一個 space；換 space 只走管理指令"
            )
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
            INSERT INTO vaults (key, display, kind, created, origin, origin_detail,
                                space)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET display = excluded.display,
                                            kind = excluded.kind
            """,
            (
                vault.key,
                vault.display,
                vault.kind,
                utc_now(),
                origin,
                origin_detail,
                vault.space,
            ),
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
    """vault（key 或別名）已存在於 `vault.space` 就回傳現行 key；
    不存在才以 `origin` 建立。

    回傳 (現行 key, 是否新建)。只給明確允許自動建立的路徑用（episode 收料、
    管線的 `global`，兩者都固定 dev）；notes 的 write 仍然不自動建
    （拼錯字不可產生幽靈範圍）。key 已存在於別的 space 時拋 `VaultConflict`。
    """
    if origin not in AUTO_ORIGINS:
        raise ValueError(f"自動建立的 origin 必須是 {sorted(AUTO_ORIGINS)}")
    with transaction(conn):
        try:
            return resolve_write(conn, vault.key, space=vault.space), False
        except UnknownVault:
            pass
        upsert_vault(conn, vault, origin=origin, origin_detail=origin_detail)
        return vault.key, True


@dataclass(frozen=True)
class EpisodeRoute:
    """episode 收料的 vault 決定結果。`origin_key` 只在改路由到雜項時有值。"""

    key: str
    origin_key: str | None
    created: bool


def route_episode_vault(
    conn: sqlite3.Connection,
    requested: str,
    *,
    display: str,
    origin_detail: str | None = None,
) -> EpisodeRoute:
    """episode 收料決定 vault（D14）。只在 dev。

    - key 或別名命中 dev 內既有 vault → 照舊（含已用 `/pm init` 正式註冊的 folder key）
    - 不存在且為 `folder/<名稱>`（沒有 git remote 的位置）→ 雜項 vault
      （首次自動建立），`origin_key` 記原 key
    - 不存在的其他 key（git remote 正規化）→ 照舊自動建立 kind=repo
    - 直接指定雜項 key → `ReservedVault`（客戶端不該送，會破壞 origin_key 對帳）
    """
    key = canonical_key(_validate_raw(requested))
    if key == ALL_VAULTS:
        raise VaultRequired("寫入必須指定單一 vault，不可用 '*'")
    if key == MISC_VAULT_KEY:
        raise ReservedVault(
            f"{MISC_VAULT_KEY!r} 保留給雜項 vault，收料時由服務端決定，客戶端不可指定"
        )
    with transaction(conn):
        try:
            return EpisodeRoute(resolve_write(conn, key, space=SPACE_DEV), None, False)
        except UnknownVault:
            pass
        if key.startswith(FOLDER_KEY_PREFIX):
            misc, created = ensure_vault(
                conn,
                Vault(key=MISC_VAULT_KEY, display=MISC_VAULT_DISPLAY, kind=KIND_MISC),
                origin=ORIGIN_EPISODE,
                origin_detail=origin_detail,
            )
            kind = conn.execute(
                "SELECT kind FROM vaults WHERE key = ?", (misc,)
            ).fetchone()[0]
            if kind != KIND_MISC:
                raise VaultConflict(
                    f"vault {misc!r} 不是雜項 vault（kind={kind!r}），拒收"
                )
            return EpisodeRoute(misc, key, created)
        created_key, created = ensure_vault(
            conn,
            Vault(key=key, display=display or key, kind="repo"),
            origin=ORIGIN_EPISODE,
            origin_detail=origin_detail,
        )
        return EpisodeRoute(created_key, None, created)


def misc_vault_keys(conn: sqlite3.Connection, *, space: str) -> frozenset[str]:
    """`space` 內雜項 vault 的 key（正常最多一個；recall／匯出降權用）。"""
    return frozenset(
        r[0]
        for r in conn.execute(
            "SELECT key FROM vaults WHERE kind = ? AND space = ?", (KIND_MISC, space)
        )
    )


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
        key=row["key"],
        display=row["display"],
        kind=row["kind"],
        aliases=aliases,
        space=row["space"],
    )


def get_vault(conn: sqlite3.Connection, vault: str, *, space: object) -> Vault:
    """在 `space` 內以 key 或別名取 vault。

    不存在（或屬於別的 space）拋 `UnknownVault`。
    """
    key = resolve_write(conn, vault, space=space)
    row = conn.execute("SELECT * FROM vaults WHERE key = ?", (key,)).fetchone()
    return _row_to_vault(conn, row)


def list_vaults(conn: sqlite3.Connection, *, space: str | None) -> list[Vault]:
    """列出 vault。`space=None` 必須明示，代表不分 space（管理／匯入等內部用途）。"""
    if space is None:
        rows = conn.execute("SELECT * FROM vaults ORDER BY key").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM vaults WHERE space = ? ORDER BY key",
            (validate_space(space),),
        ).fetchall()
    return [_row_to_vault(conn, row) for row in rows]
