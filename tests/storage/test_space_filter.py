"""A18（T-52～T-54）：space 與 vault 為 AND 疊加的硬範圍，在儲存層強制。

- space 必填、無預設；不在白名單拒絕
- key／別名存在但屬於別的 space → `UnknownVault`（不洩漏存在性）
- `vault="*"` 只涵蓋該 space 內的 vault
- `test_space_leak_test_is_load_bearing_*`：拿掉 space 條件（`*` 分支或 `_lookup`），
  同一組越界嘗試必須抓到洩漏
"""

from __future__ import annotations

import pytest

from lore_vault.schema import Note, SchemaError, Vault
from lore_vault.storage import admin, checks, fts, notes, records, vaults, vectors
from lore_vault.storage.errors import (
    InvalidSpace,
    SpaceKeyPrefixRequired,
    SpaceRequired,
    UnknownVault,
    VaultConflict,
)
from lore_vault.storage.vaults import (
    get_vault,
    list_vaults,
    resolve_read,
    resolve_write,
    upsert_vault,
)

DIM = 4
TS = "2026-09-01T00:00:00.000Z"
DEV = "folder/dev-a"
LORE = "lore/arc"
SHARED = "共同關鍵字 世界觀"


def _note(vault: str, note_id: str, title: str) -> Note:
    return Note(
        principal="xavier",
        id=note_id,
        vault=vault,
        title=title,
        body=SHARED,
        created=TS,
        updated=TS,
    )


@pytest.fixture
def two_spaces(conn):
    upsert_vault(conn, Vault(key=DEV, display="dev", aliases=("dev-alias",)))
    upsert_vault(
        conn,
        Vault(key=LORE, display="arc", space="lore", aliases=("lore/arc-old",)),
    )
    notes.insert_note(conn, DEV, _note(DEV, "d-1", "開發筆記"), space="dev")
    notes.insert_note(conn, LORE, _note(LORE, "l-1", "世界觀祕密"), space="lore")
    vectors.set_embedding(conn, DEV, "d-1", [1, 0, 0, 0], space="dev", dim=DIM)
    vectors.set_embedding(conn, LORE, "l-1", [1, 0, 0, 0], space="lore", dim=DIM)
    return conn


# ── 型別與參數驗證 ──


def test_vault_space_whitelist():
    assert Vault(key="folder/x", display="x").space == "dev"
    with pytest.raises(SchemaError):
        Vault(key="folder/x", display="x", space="work")


@pytest.mark.parametrize("bad", [None, "", "  ", 1])
def test_space_required(two_spaces, bad):
    with pytest.raises(SpaceRequired):
        resolve_read(two_spaces, DEV, space=bad)
    with pytest.raises(SpaceRequired):
        notes.list_notes(two_spaces, "*", space=bad)


def test_space_must_be_whitelisted(two_spaces):
    with pytest.raises(InvalidSpace):
        resolve_write(two_spaces, DEV, space="DEV")


def test_space_is_keyword_only_without_default(two_spaces):
    with pytest.raises(TypeError):
        notes.get_notes(two_spaces, DEV, ["d-1"])  # type: ignore[call-arg]


# ── 洩漏 ──


def _leaks(conn) -> list[str]:
    """在 dev 下對 lore 的東西做各種越界嘗試；回傳洩漏的項目。"""
    found: list[str] = []
    everything = notes.list_notes(conn, "*", space="dev")[0]
    if any(n.id == "l-1" for n in everything):
        found.append("list:*")
    lexical = fts.search_notes(conn, "*", "世界觀", space="dev")
    if any(h.note_id == "l-1" for h in lexical):
        found.append("fts:*")
    hits = vectors.search_vectors(conn, "*", [1, 0, 0, 0], space="dev", dim=DIM)
    if any(h.note_id == "l-1" for h in hits):
        found.append("vector:*")
    if notes.count_notes(conn, "*", space="dev") != 1:
        found.append("count:*")
    for name, key in (("key", LORE), ("alias", "lore/arc-old")):
        try:
            got = notes.get_notes(conn, key, ["l-1"], space="dev")
        except UnknownVault:
            continue
        found.append(f"get:{name}" if got else f"resolve:{name}")
    return found


def test_no_cross_space_leak(two_spaces):
    assert _leaks(two_spaces) == []
    # 反向也成立：lore 看不到 dev
    assert [n.id for n in notes.list_notes(two_spaces, "*", space="lore")[0]] == ["l-1"]
    with pytest.raises(UnknownVault):
        resolve_read(two_spaces, "dev-alias", space="lore")


def test_space_leak_test_is_load_bearing_wildcard(two_spaces, monkeypatch):
    """`*` 分支拿掉 space 條件（回到舊的恆真）→ 跨 space 洩漏必須被抓到。"""

    def no_space(scope, column):
        if scope.is_all:
            return "1 = 1", ()
        return f"{column} = ?", (scope.key,)

    for module in (notes, fts, vectors, records):
        monkeypatch.setattr(module, "vault_clause", no_space)
    assert set(_leaks(two_spaces)) == {"list:*", "fts:*", "vector:*", "count:*"}


def test_space_leak_test_is_load_bearing_lookup(two_spaces, monkeypatch):
    """`_lookup` 忽略 space → 用 lore 的 key／別名配 space=dev 必須被抓到。"""
    original = vaults._lookup

    def any_space(conn, key, space):
        for candidate in ("dev", "lore", "personal"):
            try:
                return original(conn, key, candidate)
            except UnknownVault:
                continue
        raise UnknownVault(key)

    monkeypatch.setattr(vaults, "_lookup", any_space)
    assert set(_leaks(two_spaces)) == {"get:key", "get:alias"}


def test_records_are_dev_only(two_spaces, make_episode):
    """episode／concept 固定 dev：lore 的 vault 不可收 episode，`*` 也只掃 dev。"""
    with pytest.raises(UnknownVault):
        records.insert_episode(two_spaces, LORE, make_episode())
    assert records.insert_episode(two_spaces, DEV, make_episode())
    rows, _ = records.list_episodes(two_spaces, "*")
    assert len(rows) == 1


# ── 建立／前綴／換 space ──


@pytest.mark.parametrize("key", ["arc", "folder/arc", "lore/", "personal/arc"])
def test_non_dev_key_needs_space_prefix(conn, key):
    with pytest.raises(SpaceKeyPrefixRequired):
        upsert_vault(conn, Vault(key=key, display="x", space="lore"))


def test_non_dev_alias_needs_space_prefix(conn):
    with pytest.raises(SpaceKeyPrefixRequired):
        upsert_vault(
            conn, Vault(key="lore/a", display="a", space="lore", aliases=("old-a",))
        )


def test_lore_vault_created_without_global(conn):
    upsert_vault(conn, Vault(key="lore/aeswir-arc", display="arc", space="lore"))
    assert [v.key for v in list_vaults(conn, space="lore")] == ["lore/aeswir-arc"]
    assert get_vault(conn, "lore/aeswir-arc", space="lore").space == "lore"


def test_upsert_cannot_move_space(two_spaces):
    with pytest.raises(VaultConflict):
        upsert_vault(two_spaces, Vault(key=LORE, display="x", space="dev"))


def test_change_space_refuses_dev_both_ways(two_spaces):
    # A20：dev 與 lore／personal 不互相轉換，兩個方向都拒絕
    with pytest.raises(admin.SpaceChangeRefused, match="A20"):
        admin.change_vault_space(two_spaces, DEV, "lore")
    with pytest.raises(admin.SpaceChangeRefused, match="A20"):
        admin.change_vault_space(two_spaces, LORE, "dev")
    with pytest.raises(UnknownVault):
        admin.change_vault_space(two_spaces, "lore/arc-old", "personal")  # 別名不接受
    assert get_vault(two_spaces, LORE, space="lore").space == "lore"
    assert get_vault(two_spaces, DEV, space="dev").space == "dev"


def test_change_space_moves_scope_and_renames(two_spaces):
    # lore → personal：key 與別名換前綴；personal 看得到、lore 看不到、舊 key 不留
    plan = admin.change_vault_space(two_spaces, LORE, "personal")
    assert (plan.new_key, plan.aliases) == (
        "personal/arc",
        (("lore/arc-old", "personal/arc-old"),),
    )
    moved = get_vault(two_spaces, "personal/arc-old", space="personal")
    assert (moved.key, moved.space, tuple(moved.aliases)) == (
        "personal/arc",
        "personal",
        ("personal/arc-old",),
    )
    assert [n.id for n in notes.list_notes(two_spaces, "*", space="personal")[0]] == [
        "l-1"
    ]
    assert notes.list_notes(two_spaces, "*", space="lore")[0] == []
    for space in ("lore", "personal"):
        with pytest.raises(UnknownVault):
            resolve_read(two_spaces, LORE, space=space)


# ── doctor 對帳 ──


def test_doctor_space_valid_values_turns_red(two_spaces):
    assert checks.space_valid_values(two_spaces).ok
    two_spaces.execute("UPDATE vaults SET space = 'bogus' WHERE key = ?", (DEV,))
    rec = checks.space_valid_values(two_spaces)
    assert rec.status == "fail"
    assert rec.counts["invalid"] == 1


def test_doctor_space_key_prefix_agreement_turns_red(two_spaces):
    assert checks.space_key_prefix_agreement(two_spaces).ok
    # 手動改 DB：dev 的 vault 被標成 personal，但 key 沒改
    two_spaces.execute("UPDATE vaults SET space = 'personal' WHERE key = ?", (DEV,))
    rec = checks.space_key_prefix_agreement(two_spaces)
    assert rec.status == "fail"
    # key 與別名都被抓到
    assert rec.counts["mismatched"] == 2
