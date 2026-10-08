"""墓碑清除（`purge-tombstones`）：預設 dry-run、依刪除時間篩選、清除後無法還原也不再擋
重新匯入、匯入對帳與 doctor 維持綠；`tombstones.summary` 資訊項與可設定的警告門檻。"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from lore_vault.cli import admin as cli
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.schema import Note, Vault
from lore_vault.storage import admin, imports
from lore_vault.storage import manage as storage_manage
from lore_vault.storage.db import connect
from lore_vault.storage.errors import NotFound
from lore_vault.storage.notes import insert_note
from lore_vault.storage.timeutil import utc_now
from lore_vault.storage.vaults import upsert_vault

TS = "2026-09-01T00:00:00.000Z"
OLD = "2026-01-01T00:00:00.000Z"
# document_tombstones 的「未過期」墓碑必須晚於 purge 的 30 天門檻（算的是真實牆鐘
# 時間，見 admin.purge_cutoff），不能用固定字面值——否則隨著實際日期前進會過期，
# 讓測試隨時間自然變紅。note_tombstones 那一側沒有這問題：a2 的墓碑是
# admin.delete_note 用當下 utc_now() 寫的，本來就不是固定值。
RECENT = utc_now()
SOURCE = "open_notebook"


def _note(vault: str, note_id: str) -> Note:
    return Note(
        principal="xavier",
        id=note_id,
        vault=vault,
        title=note_id,
        body=f"{note_id} 內文",
        summary=None,
        topics=(),
        links=(),
        supersedes=None,
        created=TS,
        updated=TS,
    )


@pytest.fixture
def db(tmp_path):
    """folder/a：note:a1、a2 為匯入（清單 2 筆），note:a3 為新增。
    刪 a1（匯入、舊墓碑）、a3（新增、舊墓碑）、a2（匯入、新墓碑），外加兩筆文件墓碑。"""
    path = tmp_path / "lore.db"
    conn = connect(path)
    try:
        upsert_vault(conn, Vault(key="folder/a", display="a", kind="repo"))
        for note_id in ("note:a1", "note:a2", "note:a3"):
            insert_note(conn, "folder/a", _note("folder/a", note_id), space="dev")
        entries = [
            imports.ManifestEntry(
                source_id=f"src-{n}",
                note_id=n,
                vault="folder/a",
                content_sha256=imports.content_sha256(n, f"{n} 內文"),
                source_updated=TS,
            )
            for n in ("note:a1", "note:a2")
        ]
        imports.record_manifest(conn, SOURCE, entries, {"folder/a": 2})
        for n in ("note:a1", "note:a2"):
            imports.mark_imported(conn, SOURCE, f"src-{n}", TS)
        for n in ("note:a1", "note:a2", "note:a3"):
            admin.delete_note(conn, "folder/a", n, space="dev", reason="test")
        with conn:
            conn.execute(
                "UPDATE note_tombstones SET deleted_at = ? "
                "WHERE note_id IN ('note:a1', 'note:a3')",
                (OLD,),
            )
            conn.executemany(
                """
                INSERT INTO document_tombstones
                    (document_id, vault, sha256, deleted_at, reason)
                VALUES (?, 'folder/a', ?, ?, 'test')
                """,
                [("doc:old", "a" * 64, OLD), ("doc:new", "b" * 64, RECENT)],
            )
    finally:
        conn.close()
    return path


def _run(db, *args) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = cli.main(["--db", str(db), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def _rows(db, sql: str) -> list:
    conn = connect(db)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def _doctor_failures(db, **settings) -> list[str]:
    conn = connect(db)
    try:
        report = default_registry().run(
            DoctorContext(settings=settings, resources={"db": conn}),
            categories=["import", "tombstones", "space", "vaults"],
        )
        return [o.name for o in report.outcomes if o.result.status is Status.FAIL]
    finally:
        conn.close()


def test_fixture_reconciles(db):
    assert _doctor_failures(db) == []


def test_dry_run_changes_nothing_and_warns(db):
    before = _rows(db, "SELECT note_id FROM note_tombstones ORDER BY 1")
    code, out = _run(db, "purge-tombstones", "--older-than-days", "30")
    assert code == 0 and out["mode"] == "dry_run"
    assert out["counts"] == {
        "note_tombstones": 2,
        "document_tombstones": 1,
        "import_manifest_rows": 1,
    }
    assert out["snapshot_bytes"] > 0
    assert out["oldest_deleted_at"] == OLD
    assert "無法還原" in out["warning"] and "匯回" in out["warning"]
    # 只有筆數與時間，不含標題或內文
    assert "內文" not in json.dumps(out, ensure_ascii=False)
    assert _rows(db, "SELECT note_id FROM note_tombstones ORDER BY 1") == before


def test_purge_old_tombstones_keeps_recent_and_reconcile_green(db):
    code, out = _run(db, "purge-tombstones", "--older-than-days", "30", "--yes")
    assert code == 0 and out["mode"] == "purged"
    assert out["purged"] == {
        "note_tombstones": 2,
        "document_tombstones": 1,
        "import_manifest_rows": 1,
    }
    assert _rows(db, "SELECT note_id FROM note_tombstones") == [("note:a2",)]
    assert _rows(db, "SELECT document_id FROM document_tombstones") == [("doc:new",)]
    # a1 的對帳清單列移除、來源筆數減一；a2 仍是「刻意刪除」
    assert _rows(db, "SELECT source_id FROM import_sources") == [("src-note:a2",)]
    assert _rows(db, "SELECT source_count FROM import_vault_counts") == [(1,)]
    conn = connect(db)
    try:
        assert imports.reconcile(conn, SOURCE).status == "pass"
        # 不再擋重新匯入：a1 不在墓碑集合內
        graves = imports.tombstones(conn, SOURCE)
        assert not graves.covers("note:a1", "src-note:a1")
        assert graves.covers("note:a2", "src-note:a2")
        # 清除後無法還原
        with pytest.raises(NotFound):
            admin.restore_note(conn, "note:a1")
    finally:
        conn.close()
    assert _doctor_failures(db) == []
    # 再跑一次：沒有可清的，冪等
    code, out = _run(db, "purge-tombstones", "--older-than-days", "30", "--yes")
    assert code == 0 and out["purged"] == {
        "note_tombstones": 0,
        "document_tombstones": 0,
        "import_manifest_rows": 0,
    }


def test_raw_tombstone_delete_without_manifest_cleanup_turns_reconcile_red(db):
    """對照組：只刪墓碑、不處理對帳清單 → 匯入對帳變紅（purge 的保護是必要的）。"""
    conn = connect(db)
    try:
        with conn:
            conn.execute("DELETE FROM note_tombstones WHERE note_id = 'note:a1'")
        assert imports.reconcile(conn, SOURCE).status == "fail"
    finally:
        conn.close()


def test_purge_kinds_filter(db):
    code, out = _run(
        db, "purge-tombstones", "--older-than-days", "0", "--kinds", "document", "--yes"
    )
    assert code == 0 and out["kinds"] == ["document"]
    assert out["purged"]["note_tombstones"] == 0
    assert _rows(db, "SELECT count(*) FROM document_tombstones") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM note_tombstones") == [(3,)]


@pytest.mark.parametrize(
    "args",
    [
        ["--older-than-days", "-1"],
        ["--older-than-days", "x"],
        ["--older-than-days", "1", "--kinds", "concept"],
        ["--older-than-days", "1", "--kinds", ""],
        [],
    ],
)
def test_purge_argument_errors_exit_2(db, args):
    with pytest.raises(SystemExit) as exc:
        _run(db, "purge-tombstones", *args)
    assert exc.value.code == 2


def test_purge_rolls_back_when_counts_disagree(db, monkeypatch):
    real = admin._delete_ids

    def short(conn, table, column, ids):
        return real(conn, table, column, ids[:-1])

    monkeypatch.setattr(admin, "_delete_ids", short)
    with pytest.raises(admin.PlanChanged):
        conn = connect(db)
        try:
            admin.purge_tombstones(conn, 30)
        finally:
            conn.close()
    assert _rows(db, "SELECT count(*) FROM note_tombstones") == [(3,)]
    assert _rows(db, "SELECT count(*) FROM import_sources") == [(2,)]


# ── doctor tombstones.summary ──

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _summary(db, **kwargs):
    conn = connect(db)
    try:
        return storage_manage.tombstone_stats(conn, now=NOW, **kwargs)
    finally:
        conn.close()


def test_summary_is_informational_by_default(db):
    rec = _summary(db)
    assert rec.status == "pass"
    assert rec.counts["note_tombstones"] == 3
    assert rec.counts["document_tombstones"] == 2
    assert rec.counts["snapshot_bytes"] > 0
    age = int((NOW - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds())
    assert rec.counts["oldest_age_seconds"] == age


def test_summary_warns_only_past_configured_thresholds(db):
    assert _summary(db, warn_age_days=365).status == "pass"
    assert _summary(db, warn_age_days=30).status == "warn"
    assert _summary(db, warn_bytes=10**9).status == "pass"
    assert _summary(db, warn_bytes=1).status == "warn"
    # doctor 經設定鍵接上
    conn = connect(db)
    try:
        report = default_registry().run(
            DoctorContext(
                settings={"now": NOW, "tombstones_warn_age_days": 30},
                resources={"db": conn},
            ),
            categories=["tombstones"],
        )
    finally:
        conn.close()
    status = {o.name: o.result.status for o in report.outcomes}
    assert status["tombstones.summary"] is Status.WARN


def test_summary_empty_database(tmp_path):
    conn = connect(tmp_path / "empty.db")
    try:
        rec = storage_manage.tombstone_stats(conn, now=NOW, warn_age_days=1)
    finally:
        conn.close()
    assert rec.status == "pass" and rec.counts["oldest_age_seconds"] == 0
