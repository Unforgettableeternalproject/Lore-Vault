"""管理端點新增的 doctor 對帳：`vaults.alias_integrity`、`tombstones.disjoint`。

每項都有「破壞資料後變紅」的測試；另證明拿掉 undelete 的「同交易刪墓碑」時
`tombstones.disjoint` 會抓到。
"""

from __future__ import annotations

import pytest

from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.storage import admin
from lore_vault.storage import documents as store
from lore_vault.storage.manage import alias_integrity, tombstones_disjoint

SHA = "b" * 64


def _doctor(conn, name: str) -> str:
    report = default_registry().run(DoctorContext(resources={"db": conn}))
    return next(o for o in report.outcomes if o.name == name).result.status.value


def test_alias_integrity_passes_on_clean_db(conn, add_vault):
    add_vault("folder/a", aliases=("folder/a-old",))
    add_vault("folder/b")
    assert alias_integrity(conn).status == "pass"
    assert _doctor(conn, "vaults.alias_integrity") == "pass"


def test_alias_integrity_fails_when_alias_shadows_key(conn, add_vault):
    add_vault("folder/a")
    add_vault("folder/b")
    # 寫入路徑會擋，直接改 DB 模擬繞過
    conn.execute(
        "INSERT INTO vault_aliases (alias, vault) VALUES ('folder/b', 'folder/a')"
    )
    rec = alias_integrity(conn)
    assert rec.status == "fail" and rec.counts["shadowing_key"] == 1
    assert _doctor(conn, "vaults.alias_integrity") == "fail"


def test_alias_integrity_fails_on_dangling_alias(conn, add_vault):
    add_vault("folder/a")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO vault_aliases (alias, vault) VALUES ('folder/x', 'nope')")
    rec = alias_integrity(conn)
    assert rec.status == "fail" and rec.counts["dangling"] == 1


def test_tombstones_disjoint_detects_note_overlap(conn, add_vault, add_note):
    add_vault("folder/a")
    add_note("folder/a", "note:1", "標題")
    assert tombstones_disjoint(conn).status == "pass"
    conn.execute(
        "INSERT INTO note_tombstones (note_id, vault, deleted_at, reason) "
        "VALUES ('note:1', 'folder/a', '2026-09-01T00:00:00.000Z', 'x')"
    )
    rec = tombstones_disjoint(conn)
    assert rec.status == "fail" and rec.counts["note_overlap"] == 1
    assert _doctor(conn, "tombstones.disjoint") == "fail"


def _deleted_document(conn) -> str:
    doc = store.insert_document(
        conn,
        "folder/a",
        space="dev",
        filename="a.md",
        mime="text/markdown",
        size_bytes=3,
        sha256=SHA,
    )
    admin.delete_document(conn, "folder/a", doc.id, space="dev")
    return doc.id


def test_undelete_document_removes_tombstone(conn, add_vault):
    add_vault("folder/a")
    doc_id = _deleted_document(conn)
    grave = admin.find_document_tombstone(conn, doc_id)
    assert grave["filename"] == "a.md" and grave["size_bytes"] == 3
    result = admin.undelete_document(conn, doc_id, space="dev", blob_ok=lambda s: True)
    assert result["document"].id == doc_id and result["document"].status == "pending"
    assert tombstones_disjoint(conn).status == "pass"


def test_undelete_without_tombstone_removal_turns_doctor_red(
    conn, add_vault, monkeypatch
):
    """拿掉「同交易刪墓碑」這道保護：對帳必須變紅。"""
    add_vault("folder/a")
    doc_id = _deleted_document(conn)
    real_execute = conn.execute

    class Conn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM document_tombstones"):
                return real_execute("SELECT 1")
            return real_execute(sql, *args)

    admin.undelete_document(Conn(), doc_id, space="dev", blob_ok=lambda s: True)
    rec = tombstones_disjoint(conn)
    assert rec.status == "fail" and rec.counts["document_overlap"] == 1


@pytest.mark.parametrize("reason", ["vault_deleted", "incomplete", "blob_missing"])
def test_undelete_document_refusals(conn, add_vault, reason):
    add_vault("folder/a")
    doc_id = _deleted_document(conn)
    blob_ok = lambda s: reason != "blob_missing"  # noqa: E731
    if reason == "vault_deleted":
        admin.delete_vault(conn, "folder/a", force=True)
    elif reason == "incomplete":
        conn.execute("UPDATE document_tombstones SET filename = NULL")
    with pytest.raises(admin.NotRestorable) as err:
        admin.undelete_document(conn, doc_id, space="dev", blob_ok=blob_ok)
    assert err.value.reason == reason
    # 拒絕時墓碑不動
    assert admin.find_document_tombstone(conn, doc_id)["document_id"] == doc_id
