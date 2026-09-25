"""儲存層對帳項：正常資料全綠，破壞後各項必須紅；並經 doctor 註冊與 CLI 驗證。"""

from __future__ import annotations

import io
import json

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main
from lore_vault.storage import checks, vectors
from lore_vault.storage.migrate import SCHEMA_VERSION

DIM = 4
STORAGE_CHECKS = {
    "storage.schema_version",
    "storage.fts_rows",
    "storage.missing_embeddings",
    "storage.vector_dimension",
}


@pytest.fixture
def healthy(conn, add_vault, add_note):
    v = add_vault("folder/chk")
    for i in range(3):
        add_note(v, f"n-{i}", f"標題 {i}", "正文")
        vectors.set_embedding(conn, v, f"n-{i}", [1, i, 0, 0], dim=DIM)
    return conn


def _run(conn, dim=DIM):
    return {
        "schema_version": checks.schema_version(conn),
        "fts_rows": checks.fts_rows(conn),
        "missing_embeddings": checks.missing_embeddings(conn),
        "vector_dimension": checks.vector_dimension(conn, dim=dim),
    }


def test_healthy_db_passes_everything(healthy):
    results = _run(healthy)
    assert {k: r.status for k, r in results.items()} == dict.fromkeys(results, "pass")
    assert results["fts_rows"].counts["notes"] == 3


def test_schema_version_goes_red_when_version_is_changed(healthy):
    healthy.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    rec = checks.schema_version(healthy)
    assert rec.status == "fail" and "比程式新" in rec.summary
    healthy.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    assert checks.schema_version(healthy).status == "fail"


def test_fts_rows_goes_red_when_a_row_is_deleted(healthy):
    healthy.execute(
        "DELETE FROM note_fts WHERE rowid = (SELECT seq FROM notes WHERE id = 'n-1')"
    )
    rec = checks.fts_rows(healthy)
    assert rec.status == "fail"
    assert rec.counts["missing"] == 1
    assert any("n-1" in d for d in rec.details)


def test_fts_rows_catches_swap_that_keeps_counts_equal(healthy):
    """列數相同但對不上（一筆缺、一筆孤兒）也要紅，不能只比數量。"""
    healthy.execute(
        "DELETE FROM note_fts WHERE rowid = (SELECT seq FROM notes WHERE id = 'n-1')"
    )
    healthy.execute(
        "INSERT INTO note_fts (rowid, title, content) VALUES (999, 'x', 'y')"
    )
    rec = checks.fts_rows(healthy)
    assert rec.counts["notes"] == rec.counts["fts_rows"]
    assert rec.status == "fail"
    assert (rec.counts["missing"], rec.counts["orphans"]) == (1, 1)


def test_missing_embeddings_goes_red_when_a_vector_is_deleted(healthy, add_note):
    healthy.execute(
        "DELETE FROM note_embeddings WHERE note_seq = "
        "(SELECT seq FROM notes WHERE id = 'n-2')"
    )
    add_note("folder/chk", "n-new", "新寫入、尚未補 embedding")
    rec = checks.missing_embeddings(healthy)
    assert rec.status == "warn"
    assert rec.counts == {"notes": 4, "missing": 2}
    assert rec.details == ("folder/chk: 2",)


def test_vector_dimension_goes_red_on_mismatch(healthy):
    vectors.set_embedding(healthy, "folder/chk", "n-0", [1, 2, 3], dim=3)
    rec = checks.vector_dimension(healthy, dim=DIM)
    assert rec.status == "fail" and rec.counts["mismatched"] == 1
    # BLOB 被截斷（宣告維度對、長度不對）也要抓到
    vectors.set_embedding(healthy, "folder/chk", "n-0", [1, 2, 3, 4], dim=DIM)
    healthy.execute(
        "UPDATE note_embeddings SET vector = substr(vector, 1, 8) WHERE note_seq = "
        "(SELECT seq FROM notes WHERE id = 'n-1')"
    )
    assert checks.vector_dimension(healthy, dim=DIM).status == "fail"


def test_checks_do_not_import_numpy():
    """doctor 可能在健康告警路徑上跑；對帳項本身只用標準庫。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(checks.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.split(".")[0] == "numpy" for name in imported)


# ── 經 doctor 框架 ──────────────────────────────────────────────────


def _doctor(conn, **settings):
    ctx = DoctorContext(settings=settings, resources={"db": conn})
    report = default_registry().run(ctx, categories=["storage"])
    return {o.name: o.result.status for o in report.outcomes}


def test_registered_in_default_registry(healthy):
    assert _doctor(healthy, embedding_dim=DIM) == dict.fromkeys(
        STORAGE_CHECKS, Status.PASS
    )


def test_doctor_turns_red_after_corruption(healthy):
    healthy.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    healthy.execute("DELETE FROM note_fts WHERE rowid = 1")
    statuses = _doctor(healthy, embedding_dim=DIM + 1)
    assert statuses["storage.schema_version"] is Status.FAIL
    assert statuses["storage.fts_rows"] is Status.FAIL
    assert statuses["storage.vector_dimension"] is Status.FAIL


def test_doctor_skips_without_db_or_dim(healthy):
    report = default_registry().run(DoctorContext(), categories=["storage"])
    assert {o.result.status for o in report.outcomes} == {Status.SKIPPED}
    assert _doctor(healthy)["storage.vector_dimension"] is Status.SKIPPED


def _cli(*argv):
    out = io.StringIO()
    code = doctor_main([*argv, "--json", "--category", "storage"], stdout=out)
    return code, json.loads(out.getvalue())


def test_cli_opens_db_readonly_and_reports(healthy, db_path):
    code, report = _cli("--db", str(db_path), "--embedding-dim", str(DIM))
    assert code == 0
    assert report["summary"]["pass"] == 4

    healthy.execute("DELETE FROM note_fts WHERE rowid = 1")
    code, report = _cli("--db", str(db_path), "--embedding-dim", str(DIM))
    assert code == 1


def test_cli_does_not_migrate_old_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()  # 版本 0 的空庫
    code, report = _cli("--db", str(path))
    assert code == 1
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["storage.schema_version"]["status"] == "fail"
    # 唯讀開啟：版本仍是 0，沒有被偷偷遷移
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        raw.close()


def test_cli_missing_db_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        doctor_main(["--db", str(tmp_path / "absent.db")], stdout=io.StringIO())
    assert exc.value.code == 2
