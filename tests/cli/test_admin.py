"""管理用刪除：dry-run 不動資料、--yes 單一交易刪除並寫墓碑、刪後對帳仍綠。"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from lore_vault.cli import admin as cli
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.schema import Note, Vault
from lore_vault.storage import admin, checks, imports
from lore_vault.storage.db import connect
from lore_vault.storage.enrichment import record_failure
from lore_vault.storage.errors import NotFound, UnknownVault
from lore_vault.storage.notes import insert_note
from lore_vault.storage.vaults import upsert_vault
from lore_vault.storage.vectors import set_embedding

DIM = 4
TS = "2026-09-01T00:00:00.000Z"
SOURCE = "open_notebook"


def _note(vault: str, note_id: str, title: str) -> Note:
    return Note(
        id=note_id,
        vault=vault,
        title=title,
        body=f"{title} 內文",
        summary=None,
        topics=(),
        links=(),
        supersedes=None,
        created=TS,
        updated=TS,
    )


@pytest.fixture
def db(tmp_path):
    """兩個 vault：folder/a（2 則匯入 + 1 則新增、別名、向量、補算紀錄）、
    folder/b（1 則匯入）。"""
    path = tmp_path / "lore.db"
    conn = connect(path)
    try:
        upsert_vault(
            conn, Vault(key="folder/a", display="a", kind="repo", aliases=("alias-a",))
        )
        upsert_vault(conn, Vault(key="folder/b", display="b", kind="repo"))
        for vault, note_id in (
            ("folder/a", "note:a1"),
            ("folder/a", "note:a2"),
            ("folder/a", "note:a3"),
            ("folder/b", "note:b1"),
        ):
            insert_note(conn, vault, _note(vault, note_id, note_id), space="dev")
            set_embedding(
                conn, vault, note_id, [1.0, 0.0, 0.0, 0.0], space="dev", dim=DIM
            )
        seq = conn.execute("SELECT seq FROM notes WHERE id = 'note:a1'").fetchone()[0]
        record_failure(
            conn,
            seq,
            "summary",
            TS,
            "boom",
            now=datetime(2026, 9, 1, tzinfo=UTC),
            max_attempts=3,
            backoff_seconds=60,
        )
        imported = {"note:a1": "folder/a", "note:a2": "folder/a", "note:b1": "folder/b"}
        entries = [
            imports.ManifestEntry(
                source_id=note_id,
                note_id=note_id,
                vault=vault,
                content_sha256=imports.content_sha256(note_id, f"{note_id} 內文"),
                source_updated=TS,
            )
            for note_id, vault in imported.items()
        ]
        imports.record_manifest(conn, SOURCE, entries, {"folder/a": 2, "folder/b": 1})
        for note_id in imported:
            imports.mark_imported(conn, SOURCE, note_id, TS)
    finally:
        conn.close()
    return path


def _run(db, *args) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = cli.main(["--db", str(db), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def _table_counts(db) -> dict[str, int]:
    conn = connect(db)
    try:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in (
                "vaults",
                "vault_aliases",
                "notes",
                "note_fts",
                "note_embeddings",
                "note_enrichment",
                "import_sources",
                "import_vault_counts",
                "note_tombstones",
            )
        }
    finally:
        conn.close()


def _green(db) -> None:
    conn = connect(db)
    try:
        assert checks.fts_rows(conn).status == "pass"
        assert imports.reconcile(conn, SOURCE).status == "pass"
        report = default_registry().run(
            DoctorContext(settings={"embedding_dim": DIM}, resources={"db": conn}),
            categories=["storage", "import"],
        )
        failed = [o.name for o in report.outcomes if o.result.status is Status.FAIL]
        assert failed == []
    finally:
        conn.close()


def test_fixture_is_green(db):
    _green(db)


def test_delete_note_dry_run_changes_nothing(db):
    before = _table_counts(db)
    code, result = _run(
        db, "delete-note", "--space", "dev", "--vault", "alias-a", "--id", "note:a1"
    )
    assert code == 0
    assert result["mode"] == "dry_run"
    assert result["vault"] == "folder/a"
    assert result["note_ids"] == ["note:a1"]
    assert result["counts"] == {
        "notes": 1,
        "fts_rows": 1,
        "embeddings": 1,
        "enrichment": 1,
        "tombstones": 1,
    }
    assert _table_counts(db) == before


def test_delete_imported_note_writes_tombstone_and_stays_green(db):
    code, result = _run(
        db,
        "delete-note",
        "--space",
        "dev",
        "--vault",
        "folder/a",
        "--id",
        "note:a1",
        "--reason",
        "過時",
        "--yes",
    )
    assert code == 0 and result["mode"] == "deleted"
    after = _table_counts(db)
    assert after["notes"] == 3
    assert after["note_fts"] == 3
    assert after["note_embeddings"] == 3
    assert after["note_enrichment"] == 0
    # 對帳清單與來源筆數不退帳：靠墓碑分辨刻意刪除
    assert after["import_sources"] == 3
    assert after["note_tombstones"] == 1
    conn = connect(db)
    try:
        count = conn.execute(
            "SELECT source_count FROM import_vault_counts WHERE vault = 'folder/a'"
        ).fetchone()[0]
        grave = admin.find_tombstone(conn, "note:a1")
        result = imports.reconcile(conn, SOURCE)
    finally:
        conn.close()
    assert count == 2
    assert grave["vault"] == "folder/a"
    assert (grave["source"], grave["source_id"]) == (SOURCE, "note:a1")
    assert grave["reason"] == "過時" and grave["deleted_at"]
    assert result.counts["deleted"] == 1 and result.counts["missing"] == 0
    assert "刻意刪除" in result.summary
    _green(db)


def test_delete_native_note_tombstone_has_no_source(db):
    code, _ = _run(
        db,
        "delete-note",
        "--space",
        "dev",
        "--vault",
        "folder/a",
        "--id",
        "note:a3",
        "--yes",
    )
    assert code == 0
    conn = connect(db)
    try:
        grave = admin.find_tombstone(conn, "note:a3")
    finally:
        conn.close()
    assert grave["source"] is None and grave["source_id"] is None
    assert grave["reason"] == admin.DEFAULT_NOTE_REASON
    _green(db)


def test_undelete_removes_tombstone_and_reconcile_reports_missing(db):
    _run(
        db,
        "delete-note",
        "--space",
        "dev",
        "--vault",
        "folder/a",
        "--id",
        "note:a1",
        "--yes",
    )
    code, result = _run(db, "undelete-note", "--id", "note:a1")
    assert code == 0 and result["mode"] == "dry_run"
    assert _table_counts(db)["note_tombstones"] == 1
    code, result = _run(db, "undelete-note", "--id", "note:a1", "--yes")
    assert code == 0 and result["mode"] == "undeleted"
    assert _table_counts(db)["note_tombstones"] == 0
    conn = connect(db)
    try:
        # 墓碑移除、尚未重匯：對帳顯示漏筆（重匯即補回）
        result = imports.reconcile(conn, SOURCE)
    finally:
        conn.close()
    assert result.status == "fail" and result.counts["missing"] == 1
    assert _run(db, "undelete-note", "--id", "note:a1", "--yes")[0] == 1


def test_output_has_no_titles_or_bodies(db):
    _code, result = _run(db, "delete-vault", "--key", "folder/a")
    text = json.dumps(result, ensure_ascii=False)
    assert "內文" not in text


def test_delete_vault_requires_force(db):
    before = _table_counts(db)
    code, result = _run(db, "delete-vault", "--key", "folder/a")
    assert code == 0 and result["requires_force"] is True
    assert result["note_ids"] == ["note:a1", "note:a2", "note:a3"]
    code, result = _run(db, "delete-vault", "--key", "folder/a", "--yes")
    assert code == 1 and result is None
    assert _table_counts(db) == before


def test_delete_vault_force_removes_everything_and_stays_green(db):
    code, result = _run(db, "delete-vault", "--key", "folder/a", "--force")
    assert code == 0 and result["mode"] == "dry_run"
    assert result["counts"]["notes"] == 3
    assert result["counts"]["aliases"] == 1
    assert result["counts"]["tombstones"] == 3

    code, result = _run(db, "delete-vault", "--key", "folder/a", "--force", "--yes")
    assert code == 0 and result["mode"] == "deleted"
    assert _table_counts(db) == {
        "vaults": 1,
        "vault_aliases": 0,
        "notes": 1,
        "note_fts": 1,
        "note_embeddings": 1,
        "note_enrichment": 0,
        "import_sources": 3,
        "import_vault_counts": 2,
        "note_tombstones": 3,
    }
    conn = connect(db)
    try:
        result = imports.reconcile(conn, SOURCE)
        vaults = {
            r[0] for r in conn.execute("SELECT DISTINCT vault FROM note_tombstones")
        }
    finally:
        conn.close()
    assert result.counts["deleted"] == 2 and result.counts["missing"] == 0
    assert vaults == {"folder/a"}
    _green(db)


def test_empty_vault_needs_no_force(db):
    conn = connect(db)
    try:
        upsert_vault(conn, Vault(key="folder/empty", display="e", kind="repo"))
    finally:
        conn.close()
    code, result = _run(db, "delete-vault", "--key", "folder/empty", "--yes")
    assert code == 0 and result["requires_force"] is False
    assert _table_counts(db)["vaults"] == 2


def test_delete_vault_rejects_alias_and_unknown(db):
    conn = connect(db)
    try:
        with pytest.raises(UnknownVault, match="別名"):
            admin.plan_vault_deletion(conn, "alias-a")
        with pytest.raises(NotFound):
            admin.plan_note_deletion(conn, "folder/b", "note:a1", space="dev")
    finally:
        conn.close()
    assert _run(db, "delete-vault", "--key", "folder/nope")[0] == 1


def test_vault_records_without_cascade_are_deleted(db):
    """episodes 參照 vaults 但沒有 CASCADE：不一併刪會撞外鍵。"""
    conn = connect(db)
    try:
        conn.execute(
            """
            INSERT INTO episodes (vault, session_id, prompt_id, turn_index, machine,
                                  data, recorded)
            VALUES ('folder/b', 's', 'p', 0, 'm', '{}', ?)
            """,
            (TS,),
        )
    finally:
        conn.close()
    code, result = _run(db, "delete-vault", "--key", "folder/b", "--force", "--yes")
    assert code == 0 and result["counts"]["episodes"] == 1
    assert _table_counts(db)["vaults"] == 1
    _green(db)


def test_missing_db_is_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    assert _run(missing, "delete-vault", "--key", "folder/a")[0] == 1
    assert not missing.exists()


def test_failed_delete_rolls_back(db, monkeypatch):
    """刪除途中出錯 → 整段 rollback（單一交易）。"""

    def boom(conn, plan, done):
        raise admin.PlanChanged("模擬核對失敗")

    monkeypatch.setattr(admin, "_verify", lambda plan, done: boom(None, plan, done))
    before = _table_counts(db)
    code, _ = _run(db, "delete-vault", "--key", "folder/a", "--force", "--yes")
    assert code == 1
    assert _table_counts(db) == before
    _green(db)


# ── 對帳要能紅：拿掉保護時會出錯 ──


def test_raw_delete_without_tombstone_turns_reconcile_red(db):
    """漏匯（沒有墓碑的消失）仍然是紅燈，不會被當成刻意刪除。"""
    conn = connect(db)
    try:
        seq = conn.execute("SELECT seq FROM notes WHERE id = 'note:a1'").fetchone()[0]
        conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))
        conn.execute("DELETE FROM notes WHERE seq = ?", (seq,))
        assert checks.fts_rows(conn).status == "pass"
        result = imports.reconcile(conn, SOURCE)
        assert result.status == "fail"
        assert result.counts["missing"] == 1 and result.counts["deleted"] == 0
    finally:
        conn.close()


def test_delete_with_tombstone_removed_turns_reconcile_red(db):
    """拿掉墓碑：admin 刪除後的狀態就等同漏匯。"""
    _run(
        db,
        "delete-note",
        "--space",
        "dev",
        "--vault",
        "folder/a",
        "--id",
        "note:a1",
        "--yes",
    )
    conn = connect(db)
    try:
        conn.execute("DELETE FROM note_tombstones")
        assert imports.reconcile(conn, SOURCE).status == "fail"
    finally:
        conn.close()


def test_raw_delete_without_fts_cleanup_turns_fts_red(db):
    conn = connect(db)
    try:
        conn.execute("DELETE FROM notes WHERE id = 'note:a3'")
        assert checks.fts_rows(conn).status == "fail"
    finally:
        conn.close()


def test_cascade_guard_catches_orphans(db):
    """外鍵沒開時 CASCADE 不生效：孤兒檢查讓刪除 rollback。"""
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        before = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
        with pytest.raises(admin.PlanChanged, match="孤兒"):
            admin.delete_note(conn, "folder/a", "note:a2", space="dev")
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == before
    finally:
        conn.close()


# ── set-space（A19／D-space-3：只走管理指令）──


def _space_of(db, key: str) -> str:
    conn = connect(db)
    try:
        return conn.execute(
            "SELECT space FROM vaults WHERE key = ?", (key,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_delete_note_requires_space(db, capsys):
    with pytest.raises(SystemExit) as info:
        _run(db, "delete-note", "--vault", "folder/a", "--id", "note:a1")
    assert info.value.code == 2
    # 在錯的 space 找不到（不洩漏、不刪）
    code, _ = _run(
        db, "delete-note", "--space", "lore", "--vault", "folder/a", "--id", "note:a1"
    )
    assert code == 1


def test_set_space_dry_run_then_apply(db):
    conn = connect(db)
    try:
        upsert_vault(conn, Vault(key="lore/arc", display="arc", space="lore"))
    finally:
        conn.close()
    code, result = _run(db, "set-space", "--key", "lore/arc", "--space", "dev")
    assert code == 0
    assert result["mode"] == "dry_run" and result["from"] == "lore"
    assert _space_of(db, "lore/arc") == "lore"
    code, result = _run(db, "set-space", "--key", "lore/arc", "--space", "dev", "--yes")
    assert code == 0 and result == {
        "mode": "changed",
        "key": "lore/arc",
        "from": "lore",
        "to": "dev",
    }
    assert _space_of(db, "lore/arc") == "dev"


def test_set_space_rejects_prefix_mismatch_and_aliases(db):
    # dev 的 folder/a 不能直接搬到 lore（key 沒有 lore/ 前綴）；dry-run 就擋
    for extra in ((), ("--yes",)):
        code, _ = _run(db, "set-space", "--key", "folder/a", "--space", "lore", *extra)
        assert code == 1
    assert _space_of(db, "folder/a") == "dev"
    code, _ = _run(db, "set-space", "--key", "alias-a", "--space", "dev")
    assert code == 1
