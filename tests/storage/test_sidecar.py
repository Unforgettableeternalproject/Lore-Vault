"""側載小型機器狀態（schema v17 `sidecar_blobs`）：遷移、單列覆寫、範圍、
vault 刪除與換 space 的同交易處理，以及 doctor `sidecar.orphans`。

「拿掉保護會紅」：
- `test_delete_vault_without_sidecar_step_is_rolled_back`：拿掉刪除步驟 → 筆數核對抓到
- `test_orphans_red_when_delete_vault_skips_sidecar`：整段拿掉側載處理 → doctor fail
- `test_move_space_without_space_update_is_rolled_back`：拿掉改 space → 筆數核對抓到
- `test_orphans_red_when_move_space_skips_sidecar`：整段拿掉 space 改寫 → doctor fail
"""

from __future__ import annotations

import sqlite3

import pytest

from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.schema import Vault
from lore_vault.storage import admin, sidecar
from lore_vault.storage.errors import NotFound, UnknownVault, VaultRequired
from lore_vault.storage.migrate import MIGRATIONS, SCHEMA_VERSION, migrate
from lore_vault.storage.vaults import upsert_vault

DEV = "folder/side"
KEY = "tasks-snapshot"


def _doctor(conn) -> str:
    report = default_registry().run(
        DoctorContext(resources={"db": conn}), categories=["sidecar"]
    )
    return next(o for o in report.outcomes if o.name == "sidecar.orphans").result


def _status(conn) -> str:
    return _doctor(conn).status.value


def _lore_vault(conn, key: str = "lore/arc", aliases=("lore/arc-old",)) -> str:
    upsert_vault(
        conn, Vault(key=key, display=key, kind="repo", aliases=aliases, space="lore")
    )
    return key


# ── 遷移 ──


def test_v17_adds_sidecar_table_and_keeps_data(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        migrate(raw, migrations=MIGRATIONS[:16])
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/x', 'x', 'repo', '2026-01-01T00:00:00.000Z')"
        )
        assert not sidecar.has_table(raw)
        assert migrate(raw) == SCHEMA_VERSION == 17
        cols = {r["name"]: r for r in raw.execute("PRAGMA table_info(sidecar_blobs)")}
        assert set(cols) == {"vault", "space", "key", "mime", "content", "updated"}
        pk = sorted((r["pk"], r["name"]) for r in cols.values() if r["pk"])
        assert [name for _, name in pk] == ["vault", "key"]
        # 刻意無外鍵（由 admin 同交易處理、doctor 對帳）
        assert raw.execute("PRAGMA foreign_key_list(sidecar_blobs)").fetchall() == []
        assert raw.execute("SELECT key FROM vaults").fetchall()[0][0] == "folder/x"
    finally:
        raw.close()


# ── 讀寫 ──


def test_put_get_roundtrip_and_overwrite(conn, add_vault):
    add_vault(DEV)
    first = sidecar.put(
        conn, DEV, KEY, b'{"v":1}', space="dev", mime="application/json"
    )
    got = sidecar.get(conn, DEV, KEY, space="dev")
    assert got.content == b'{"v":1}' and got.mime == "application/json"
    assert got.updated == first.updated
    sidecar.put(conn, DEV, KEY, b'{"v":2}', space="dev")
    got = sidecar.get(conn, DEV, KEY, space="dev")
    # 單列語意：舊內容不可讀、mime 也一併覆寫
    assert got.content == b'{"v":2}' and got.mime == sidecar.DEFAULT_MIME
    assert conn.execute("SELECT count(*) FROM sidecar_blobs").fetchone()[0] == 1


def test_put_via_alias_stores_canonical_key(conn, add_vault):
    add_vault(DEV, aliases=("folder/side-old",))
    blob = sidecar.put(conn, "folder/side-old", KEY, b"x", space="dev")
    assert blob.vault == DEV
    assert sidecar.get(conn, DEV, KEY, space="dev").content == b"x"


def test_scope_errors(conn, add_vault):
    add_vault(DEV)
    _lore_vault(conn)
    with pytest.raises(UnknownVault):
        sidecar.put(conn, "folder/none", KEY, b"x", space="dev")
    # 別的 space 的 vault 與不存在相同
    with pytest.raises(UnknownVault):
        sidecar.put(conn, "lore/arc", KEY, b"x", space="dev")
    with pytest.raises(VaultRequired):
        sidecar.put(conn, None, KEY, b"x", space="dev")
    with pytest.raises(VaultRequired):
        sidecar.put(conn, "*", KEY, b"x", space="dev")
    with pytest.raises(NotFound):
        sidecar.get(conn, DEV, KEY, space="dev")


@pytest.mark.parametrize(
    "key", ["", "a/b", "a\\b", "..", ".hidden", "a b", "鍵", "x" * 129]
)
def test_invalid_keys(conn, add_vault, key):
    add_vault(DEV)
    with pytest.raises(sidecar.InvalidSidecarKey):
        sidecar.put(conn, DEV, key, b"x", space="dev")


def test_size_limit(conn, add_vault):
    add_vault(DEV)
    sidecar.put(conn, DEV, KEY, b"x" * sidecar.MAX_BYTES, space="dev")
    with pytest.raises(sidecar.SidecarTooLarge):
        sidecar.put(conn, DEV, KEY, b"x" * (sidecar.MAX_BYTES + 1), space="dev")
    # 拒絕時不截斷、不覆寫
    assert len(sidecar.get(conn, DEV, KEY, space="dev").content) == sidecar.MAX_BYTES


def test_list_for_key_is_space_scoped_and_skips_orphans(conn, add_vault):
    add_vault("folder/b")
    add_vault("folder/a")
    _lore_vault(conn)
    sidecar.put(conn, "folder/b", KEY, b"b", space="dev")
    sidecar.put(conn, "folder/a", KEY, b"a", space="dev")
    sidecar.put(conn, "folder/a", "other", b"o", space="dev")
    sidecar.put(conn, "lore/arc", KEY, b"l", space="lore")
    items = sidecar.list_for_key(conn, KEY, space="dev")
    assert [(b.vault, b.content) for b in items] == [
        ("folder/a", b"a"),
        ("folder/b", b"b"),
    ]
    assert [b.vault for b in sidecar.list_for_key(conn, KEY, space="lore")] == [
        "lore/arc"
    ]
    assert sidecar.list_for_key(conn, KEY, space="personal") == []
    # 孤兒列（vault 被繞過刪除）不回傳
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM vault_aliases WHERE vault = 'folder/b'")
    conn.execute("DELETE FROM vaults WHERE key = 'folder/b'")
    assert [b.vault for b in sidecar.list_for_key(conn, KEY, space="dev")] == [
        "folder/a"
    ]


# ── doctor sidecar.orphans ──


def test_orphans_pass_on_clean_db(conn, add_vault):
    add_vault(DEV)
    sidecar.put(conn, DEV, KEY, b"x", space="dev")
    assert _status(conn) == "pass"


def test_orphans_skipped_before_v17(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    try:
        migrate(raw, migrations=MIGRATIONS[:16])
        assert _status(raw) == "skipped"
    finally:
        raw.close()


def test_orphans_fail_on_dangling_and_space_mismatch(conn, add_vault):
    add_vault(DEV)
    add_vault("folder/gone")
    sidecar.put(conn, DEV, KEY, b"x", space="dev")
    sidecar.put(conn, "folder/gone", KEY, b"x", space="dev")
    conn.execute("DELETE FROM vaults WHERE key = 'folder/gone'")
    conn.execute("UPDATE sidecar_blobs SET space = 'lore' WHERE vault = ?", (DEV,))
    result = _doctor(conn)
    assert result.status.value == "fail"
    assert result.counts == {"rows": 2, "dangling": 1, "space_mismatch": 1}


# ── vault 刪除 ──


def test_delete_vault_removes_sidecar_in_same_transaction(conn, add_vault):
    add_vault(DEV)
    add_vault("folder/keep")
    sidecar.put(conn, DEV, KEY, b"x", space="dev")
    sidecar.put(conn, "folder/keep", KEY, b"k", space="dev")
    plan = admin.plan_vault_deletion(conn, DEV)
    assert plan.counts["sidecar_blobs"] == 1
    # 側載是可重建的機器狀態：不要求 force
    assert plan.requires_force is False
    admin.delete_vault(conn, DEV)
    assert sidecar.count_for_vault(conn, DEV) == 0
    assert sidecar.count_for_vault(conn, "folder/keep") == 1
    assert _status(conn) == "pass"


def test_delete_vault_without_sidecar_step_is_rolled_back(conn, add_vault, monkeypatch):
    """拿掉「連帶刪除側載列」：實際刪除筆數與規劃不符，整段 rollback。"""
    add_vault(DEV)
    sidecar.put(conn, DEV, KEY, b"x", space="dev")
    monkeypatch.setattr(admin.sidecar, "delete_for_vault", lambda c, v: 1)
    with pytest.raises(admin.PlanChanged):
        admin.delete_vault(conn, DEV)
    assert conn.execute("SELECT 1 FROM vaults WHERE key = ?", (DEV,)).fetchone()
    assert sidecar.count_for_vault(conn, DEV) == 1


def test_orphans_red_when_delete_vault_skips_sidecar(conn, add_vault, monkeypatch):
    """整段拿掉側載處理（規劃與刪除都不知道這張表）：留下孤兒，doctor 變紅。"""
    add_vault(DEV)
    sidecar.put(conn, DEV, KEY, b"x", space="dev")
    with monkeypatch.context() as m:
        m.setattr(admin.sidecar, "has_table", lambda c: False)
        admin.delete_vault(conn, DEV)
    assert _status(conn) == "fail"


# ── 換 space ──


def test_move_space_rewrites_vault_and_space(conn):
    _lore_vault(conn)
    sidecar.put(conn, "lore/arc", KEY, b"x", space="lore")
    plan = admin.plan_space_change(conn, "lore/arc", "personal")
    assert plan.counts["sidecar_blobs.vault"] == 1
    assert plan.counts["sidecar_blobs.space"] == 1
    admin.change_vault_space(conn, "lore/arc", "personal")
    rows = conn.execute("SELECT vault, space FROM sidecar_blobs").fetchall()
    assert [tuple(r) for r in rows] == [("personal/arc", "personal")]
    assert sidecar.get(conn, "personal/arc", KEY, space="personal").content == b"x"
    assert sidecar.list_for_key(conn, KEY, space="lore") == []
    assert _status(conn) == "pass"


def test_move_space_without_space_update_is_rolled_back(conn, monkeypatch):
    _lore_vault(conn)
    sidecar.put(conn, "lore/arc", KEY, b"x", space="lore")
    monkeypatch.setattr(admin.sidecar, "set_space_for_vault", lambda c, v, s: 1)
    with pytest.raises(admin.PlanChanged):
        admin.change_vault_space(conn, "lore/arc", "personal")
    rows = conn.execute("SELECT vault, space FROM sidecar_blobs").fetchall()
    assert [tuple(r) for r in rows] == [("lore/arc", "lore")]


def test_orphans_red_when_move_space_skips_sidecar(conn, monkeypatch):
    """拿掉 space 欄的改寫（key 欄仍由動態偵測改名）：space 不符，doctor 變紅。"""
    _lore_vault(conn)
    sidecar.put(conn, "lore/arc", KEY, b"x", space="lore")
    with monkeypatch.context() as m:
        m.setattr(admin.sidecar, "has_table", lambda c: False)
        admin.change_vault_space(conn, "lore/arc", "personal")
    result = _doctor(conn)
    assert result.status.value == "fail" and result.counts["space_mismatch"] == 1


def test_sidecar_vault_column_is_a_known_reference(conn):
    assert ("sidecar_blobs", "vault") in admin.vault_reference_columns(conn)
    assert ("sidecar_blobs", "vault") in admin.KNOWN_VAULT_REFERENCES


def test_sidecar_is_excluded_from_snapshot():
    from lore_vault.storage import snapshot

    assert sidecar.TABLE in snapshot.SNAPSHOT_EXCLUDED_TABLES
    assert sidecar.TABLE not in snapshot.SNAPSHOT_TABLES
