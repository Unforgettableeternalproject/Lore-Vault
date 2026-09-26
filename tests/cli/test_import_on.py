"""T-33～T-37：Open Notebook 匯入（fake ON API，不打真服務）。"""

from __future__ import annotations

import io
import json
import urllib.parse
from pathlib import Path

import pytest

from lore_vault.cli import import_on as mod
from lore_vault.cli import on_orphans
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main
from lore_vault.storage import admin, checks, imports
from lore_vault.storage.db import connect
from lore_vault.storage.notes import get_note, update_note_if

TS = "2026-05-18 03:18:02.289306+00:00"


# ── fake ON ─────────────────────────────────────────────────────────


class FakeOn:
    """模擬 ON 1.14.0：list 不回內容、全量端點可設為 500、只接受 GET。"""

    def __init__(self) -> None:
        self.notebooks: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.membership: dict[str, list[str]] = {}
        self.all_notes_status = 200
        self.note_count_override: dict[str, int] = {}
        self.drop_from_listing: dict[str, str] = {}
        self.calls: list[str] = []

    def notebook(self, nb_id: str, name: str, description: str = "") -> str:
        self.notebooks.append(
            {
                "id": nb_id,
                "name": name,
                "description": description,
                "archived": False,
                "created": TS,
                "updated": TS,
            }
        )
        self.membership.setdefault(nb_id, [])
        return nb_id

    def note(
        self,
        note_id: str,
        title: str,
        content: str = "內容",
        *,
        notebooks: tuple[str, ...] = (),
        created: str = TS,
        updated: str = TS,
    ) -> None:
        self.notes[note_id] = {
            "id": note_id,
            "title": title,
            "content": content,
            "note_type": "human",
            "created": created,
            "updated": updated,
            "command_id": None,
        }
        for nb in notebooks:
            self.membership[nb].append(note_id)

    def _json(self, data) -> tuple[int, bytes]:
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __call__(self, url, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/notebooks":
            return self._json(
                [
                    {
                        **nb,
                        "source_count": 0,
                        "note_count": self.note_count_override.get(
                            nb["id"], len(self.membership[nb["id"]])
                        ),
                    }
                    for nb in self.notebooks
                ]
            )
        if path == "/api/notes" and "notebook_id" in query:
            nb = query["notebook_id"][0]
            ids = [
                i for i in self.membership[nb] if self.drop_from_listing.get(i) != nb
            ]
            return self._json([{**self.notes[i], "content": None} for i in ids])
        if path == "/api/notes":
            if self.all_notes_status != 200:
                return self.all_notes_status, b'{"detail":"Serialization error"}'
            return self._json(list(self.notes.values()))
        if path.startswith("/api/notes/"):
            note_id = urllib.parse.unquote(path[len("/api/notes/") :])
            if note_id not in self.notes:
                return 404, b'{"detail":"Note not found"}'
            return self._json(self.notes[note_id])
        return 404, b'{"detail":"Not Found"}'


@pytest.fixture
def fake() -> FakeOn:
    on = FakeOn()
    on.notebook("notebook:a", "[PM] Alpha", "desc [bind: github.com/U/Alpha]")
    on.notebook("notebook:b", "[PM] Beta", "沒有綁定標記")
    on.notebook("notebook:g", "[PM] Global — 跨專案觀察", "")
    on.notebook("notebook:e", "[PM] Empty", "[bind: folder/Empty]")
    on.note(
        "note:a1",
        "Alpha 決策",
        "見 [[Alpha 細節]] 與 [[Beta 筆記]]",
        notebooks=("notebook:a",),
    )
    on.note(
        "note:a2",
        "Alpha 細節",
        "回指 [[alpha 決策|別名]]、[[不存在]]、[[重複]]",
        notebooks=("notebook:a",),
    )
    on.note("note:a3", "重複", notebooks=("notebook:a",))
    on.note("note:a4", "重複", notebooks=("notebook:a",))
    on.note("note:b1", "Beta 筆記", "[[Beta 筆記]] 自己", notebooks=("notebook:b",))
    on.note("note:g1", "全域觀察", notebooks=("notebook:g",))
    return on


def _export(fake: FakeOn, out: Path) -> dict:
    return mod.export_on(mod.OnClient(getter=fake), out)


def _prepare(fake: FakeOn, tmp_path: Path):
    out = tmp_path / "export"
    _export(fake, out)
    export = mod.load_export(out)
    mapping = mod.build_mapping(export)
    return export, mapping


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lore.db")
    yield connection
    connection.close()


def _reconcile(conn):
    report = default_registry().run(
        DoctorContext(resources={"db": conn}), categories=["import"]
    )
    return report.outcomes[0].result


# ── T-33 匯出 ───────────────────────────────────────────────────────


def test_export_counts_match_and_only_get(fake, tmp_path):
    manifest = _export(fake, tmp_path / "out")
    assert manifest["counts"]["notes"] == 6
    assert manifest["counts"]["on_note_count_total"] == 6
    assert manifest["all_notes_endpoint"]["status"] == 200
    lines = (tmp_path / "out" / mod.NOTES_FILE).read_text("utf-8").splitlines()
    assert len(lines) == 6
    assert all(json.loads(line)["content"] is not None for line in lines)


def test_export_fails_when_listing_is_short_of_note_count(fake, tmp_path):
    # ON 自己回報 4 則，但列表只回 3 則（截斷／隱性分頁）→ 必須失敗、不寫檔
    fake.drop_from_listing["note:a4"] = "notebook:a"
    fake.note_count_override["notebook:a"] = 4
    with pytest.raises(mod.OnImportError, match="note_count=4"):
        _export(fake, tmp_path / "out")
    assert not (tmp_path / "out" / mod.NOTES_FILE).exists()


def test_export_fails_when_note_count_disagrees(fake, tmp_path):
    fake.note_count_override["notebook:b"] = 2
    with pytest.raises(mod.OnImportError, match="notebook:b"):
        _export(fake, tmp_path / "out")


def test_export_falls_back_to_per_note_get_when_all_notes_fails(fake, tmp_path):
    fake.all_notes_status = 500
    manifest = _export(fake, tmp_path / "out")
    assert manifest["counts"]["notes"] == 6
    assert manifest["counts"]["fetched_individually"] == 6
    assert manifest["all_notes_endpoint"]["orphans_enumerable"] is False
    export = mod.load_export(tmp_path / "out")
    assert all(n["content"] for n in export.notes)


def test_export_records_orphans_and_multi_membership(fake, tmp_path):
    fake.note("note:orphan", "孤兒")
    fake.membership["notebook:b"].append("note:a1")  # a1 同時屬於 a、b
    manifest = _export(fake, tmp_path / "out")
    assert manifest["orphans"] == ["note:orphan"]
    assert manifest["multi_membership"] == [
        {"id": "note:a1", "notebooks": ["notebook:a", "notebook:b"]}
    ]
    assert manifest["counts"]["notes"] == 7


def test_load_export_rejects_modified_file(fake, tmp_path):
    _export(fake, tmp_path / "out")
    path = tmp_path / "out" / mod.NOTES_FILE
    path.write_text(path.read_text("utf-8").replace("內容", "改過"), encoding="utf-8")
    with pytest.raises(mod.OnImportError, match="雜湊"):
        mod.load_export(tmp_path / "out")


def test_outputs_inside_repo_are_refused(fake):
    repo = Path(mod.__file__).resolve().parents[3]
    code = mod.main(
        ["export", "--out", str(repo / "tmp-export")], getter=fake, stdout=io.StringIO()
    )
    assert code == 1
    assert not (repo / "tmp-export").exists()


# ── T-34 mapping ────────────────────────────────────────────────────


def test_mapping_bind_name_global_and_review(fake, tmp_path):
    _, mapping = _prepare(fake, tmp_path)
    by_id = {e["on_id"]: e for e in mapping["notebooks"]}
    assert by_id["notebook:a"]["key"] == "github.com/u/alpha"
    assert by_id["notebook:a"]["needs_review"] is False
    assert by_id["notebook:e"]["key"] == "folder/empty"
    assert by_id["notebook:b"]["key"] == "folder/beta"
    assert by_id["notebook:b"]["needs_review"] is True
    assert by_id["notebook:g"]["kind"] == "global"
    assert by_id["notebook:g"]["key"] == mod.GLOBAL_KEY
    items = mod.review_items(mapping)
    assert len(items) == 1 and "notebook:b" in items[0]


def test_mapping_flags_shared_key_and_multi_membership(fake, tmp_path):
    fake.notebook("notebook:a2", "[PM] Alpha copy", "[bind: github.com/u/alpha]")
    fake.membership["notebook:b"].append("note:a1")
    _, mapping = _prepare(fake, tmp_path)
    by_id = {e["on_id"]: e for e in mapping["notebooks"]}
    assert by_id["notebook:a"]["needs_review"] and by_id["notebook:a2"]["needs_review"]
    assert mapping["note_assignments"]["note:a1"]["needs_review"] is True


def test_import_refuses_unreviewed_mapping(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    with pytest.raises(mod.OnImportError, match="待人工確認"):
        mod.run_import(conn, export, mapping)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


# ── 匯入、連結、冪等 ────────────────────────────────────────────────


def _reviewed(mapping):
    for e in mapping["notebooks"]:
        e["needs_review"] = False
    for a in mapping["note_assignments"].values():
        a["needs_review"] = False
    return mapping


def test_import_creates_vaults_notes_and_keeps_timestamps(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, _reviewed(mapping))
    assert report["notes"]["inserted"] == 6
    assert sorted(report["vaults"]["created"]) == [
        "folder/beta",
        "folder/empty",
        "github.com/u/alpha",
        "global",
    ]
    kinds = dict(conn.execute("SELECT key, kind FROM vaults").fetchall())
    assert kinds["global"] == "global"
    note = get_note(conn, "github.com/u/alpha", "note:a1")
    assert note.created == note.updated == "2026-05-18T03:18:02.289Z"
    assert note.summary is None
    assert conn.execute("SELECT count(*) FROM note_embeddings").fetchone()[0] == 0
    assert report["per_vault"]["folder/empty"] == 0


def test_link_to_title_with_brackets():
    assert mod.link_targets("見 [[[Decision] 選 SQLite]] 與 [[一般]]") == [
        "[Decision] 選 SQLite",
        "一般",
    ]


def test_links_resolve_within_vault_and_report_the_rest(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, _reviewed(mapping))
    a1 = get_note(conn, "github.com/u/alpha", "note:a1")
    a2 = get_note(conn, "github.com/u/alpha", "note:a2")
    assert a1.links == ("note:a2",)
    assert a2.links == ("note:a1",)  # 大小寫與 |別名 都能解析
    assert "[[Beta 筆記]]" in a1.body  # 原文保留
    by_status = report["links"]["by_status"]
    assert by_status == {
        "ambiguous": 1,
        "cross_vault": 1,
        "resolved": 2,
        "self": 1,
        "unresolved": 1,
    }
    flagged = {(e["target"], e["status"]) for e in report["links"]["entries"]}
    assert flagged == {
        ("Beta 筆記", "cross_vault"),
        ("不存在", "unresolved"),
        ("重複", "ambiguous"),
    }


def test_multi_membership_uses_assignment(fake, tmp_path, conn):
    fake.membership["notebook:b"].append("note:a1")
    export, mapping = _prepare(fake, tmp_path)
    mapping = _reviewed(mapping)
    mapping["note_assignments"]["note:a1"]["assigned"] = "notebook:b"
    report = mod.run_import(conn, export, mapping)
    assert get_note(conn, "folder/beta", "note:a1").vault == "folder/beta"
    assert report["per_vault"]["folder/beta"] == 2
    assert len(report["multi_membership"]) == 1
    assert _reconcile(conn).status is Status.PASS


def test_orphans_are_skipped_and_reported(fake, tmp_path, conn):
    fake.note("note:orphan", "孤兒")
    export, mapping = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, _reviewed(mapping))
    assert report["skipped"]["orphan"] == 1
    assert mapping["orphans"] == ["note:orphan"]


def test_rerun_is_idempotent(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["inserted"] == 0
    assert report["notes"]["unchanged"] == 6
    assert report["vaults"]["created"] == []
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 6
    assert report["manifest"] == {"added": 0, "changed": 0, "removed": 0}


def test_rerun_does_not_overwrite_locally_modified_note(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    current = get_note(conn, "folder/beta", "note:b1")
    update_note_if(
        conn, "folder/beta", "note:b1", current.updated, {"body": "新系統改"}
    )
    # 來源端也改了：仍不覆寫本地修改
    fake.notes["note:b1"]["content"] = "ON 端也改了"
    fake.notes["note:b1"]["updated"] = "2026-09-01 00:00:00.000000+00:00"
    export, _ = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["modified_locally"] == ["note:b1"]
    assert get_note(conn, "folder/beta", "note:b1").body == "新系統改"
    # 合法修改（updated 推進）只報告、不算竄改
    result = _reconcile(conn)
    assert result.status is Status.PASS
    assert result.counts["modified_after_import"] == 1


def test_rerun_applies_source_change_when_not_modified_locally(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    fake.notes["note:g1"]["content"] = "ON 端更新"
    fake.notes["note:g1"]["updated"] = "2026-09-01 00:00:00.123456+00:00"
    export, _ = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["updated_from_source"] == 1
    note = get_note(conn, "global", "note:g1")
    assert note.body == "ON 端更新" and note.updated == "2026-09-01T00:00:00.123Z"
    assert _reconcile(conn).status is Status.PASS


def test_timestamp_anomalies_are_reported(fake, tmp_path, conn):
    fake.note(
        "note:t1",
        "時區",
        notebooks=("notebook:g",),
        created="2026-05-18 11:00:00+08:00",
        updated="2026-05-18 02:00:00",
    )
    export, mapping = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, _reviewed(mapping))
    kinds = {(a["field"], a["kind"]) for a in report["timestamp_anomalies"]}
    assert kinds == {
        ("created", "offset"),
        ("updated", "naive"),
        ("updated", "before_created"),
    }
    note = get_note(conn, "global", "note:t1")
    assert note.created == note.updated == "2026-05-18T03:00:00.000Z"


# ── T-37 對帳 ───────────────────────────────────────────────────────


def test_reconcile_is_skipped_before_any_import(conn):
    assert _reconcile(conn).status is Status.SKIPPED


def test_reconcile_passes_after_import_and_reports_extras(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    from lore_vault.schema import Note
    from lore_vault.storage.notes import insert_note

    ts = "2026-09-26T00:00:00.000Z"
    insert_note(
        conn,
        "global",
        Note(id="new-1", vault="global", title="新增", body="", created=ts, updated=ts),
    )
    result = _reconcile(conn)
    assert result.status is Status.PASS
    assert result.counts["extra_notes"] == 1
    assert result.counts["present"] == 6


def test_reconcile_goes_red_when_one_note_is_missing(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    seq = conn.execute("SELECT seq FROM notes WHERE id = 'note:a3'").fetchone()[0]
    conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))
    conn.execute("DELETE FROM notes WHERE seq = ?", (seq,))
    result = _reconcile(conn)
    assert result.status is Status.FAIL
    assert result.counts["missing"] == 1
    assert any("note:a3" in d for d in result.details)
    # 重跑匯入補回漏筆後恢復綠燈
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["reinserted"] == 1
    assert _reconcile(conn).status is Status.PASS


# ── 墓碑：管理指令刪除的 note／vault 不匯回 ──────────────────────────


def test_admin_deleted_note_is_not_reimported(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    admin.delete_note(conn, "folder/beta", "note:b1")
    result = _reconcile(conn)
    assert result.status is Status.PASS
    assert result.counts["deleted"] == 1

    report = mod.run_import(conn, export, mapping)
    assert report["skipped"]["deleted"] == 1
    assert report["deleted_skipped"] == ["note:b1"]
    assert report["notes"]["reinserted"] == 0
    assert mod.report_summary(report)["skipped"]["deleted"] == 1
    assert conn.execute("SELECT 1 FROM notes WHERE id = 'note:b1'").fetchone() is None
    result = _reconcile(conn)
    assert result.status is Status.PASS
    assert result.counts["deleted"] == 1 and result.counts["missing"] == 0


def test_without_tombstone_check_reimport_revives_deleted_note(
    fake, tmp_path, conn, monkeypatch
):
    """拿掉匯入端的墓碑檢查：刻意刪除的 note 會被匯回來。"""
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    admin.delete_note(conn, "folder/beta", "note:b1")
    monkeypatch.setattr(
        imports,
        "tombstones",
        lambda conn, source: imports.Tombstones(frozenset(), frozenset()),
    )
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["reinserted"] == 1
    assert conn.execute("SELECT 1 FROM notes WHERE id = 'note:b1'").fetchone()


def test_deleted_vault_is_not_recreated_on_reimport(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    admin.delete_vault(conn, "github.com/u/alpha", force=True)
    assert _reconcile(conn).status is Status.PASS

    report = mod.run_import(conn, export, mapping)
    assert report["vaults"]["deleted_skipped"] == ["github.com/u/alpha"]
    assert report["vaults"]["created"] == []
    assert report["skipped"]["deleted"] == 4
    assert (
        conn.execute("SELECT 1 FROM vaults WHERE key = 'github.com/u/alpha'").fetchone()
        is None
    )
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    result = _reconcile(conn)
    assert result.status is Status.PASS
    assert result.counts["deleted"] == 4


def test_undeleted_note_comes_back_on_reimport(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    admin.delete_note(conn, "folder/beta", "note:b1")
    admin.undelete_note(conn, "note:b1")
    assert _reconcile(conn).status is Status.FAIL
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["reinserted"] == 1
    assert report["skipped"]["deleted"] == 0
    assert _reconcile(conn).status is Status.PASS


def test_reconcile_goes_red_when_import_stops_midway(fake, tmp_path, conn, monkeypatch):
    """清單先落地：中途失敗時 doctor 看得出沒匯進來的筆數。"""
    export, mapping = _prepare(fake, tmp_path)
    real = mod._import_one
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模擬中途失敗")
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "_import_one", flaky)
    with pytest.raises(RuntimeError):
        mod.run_import(conn, export, _reviewed(mapping))
    result = _reconcile(conn)
    assert result.status is Status.FAIL
    assert result.counts["missing"] == 4


def test_reconcile_goes_red_when_one_hash_is_tampered(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    conn.execute("UPDATE notes SET body = body || '!' WHERE id = 'note:g1'")
    result = _reconcile(conn)
    assert result.status is Status.FAIL
    assert result.counts["tampered"] == 1
    # 重跑匯入不自動修竄改，列入報告
    report = mod.run_import(conn, export, mapping)
    assert report["notes"]["db_tampered"] == ["note:g1"]


def test_reconcile_goes_red_when_manifest_hash_is_altered(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    conn.execute(
        "UPDATE import_sources SET content_sha256 = ? WHERE source_id = 'note:a1'",
        ("0" * 64,),
    )
    assert _reconcile(conn).status is Status.FAIL


def test_reconcile_goes_red_when_vault_count_disagrees(fake, tmp_path, conn):
    export, mapping = _prepare(fake, tmp_path)
    mod.run_import(conn, export, _reviewed(mapping))
    conn.execute(
        "UPDATE import_vault_counts SET source_count = source_count + 1 "
        "WHERE vault = 'global'"
    )
    result = _reconcile(conn)
    assert result.status is Status.FAIL and result.counts["count_mismatch"] == 1


def test_reconcile_skipped_on_pre_v3_database(tmp_path):
    import sqlite3

    from lore_vault.storage import migrate as migrate_mod

    raw = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    migrate_mod.migrate(raw, migrations=migrate_mod.MIGRATIONS[:2])
    result = _reconcile(raw)
    raw.close()
    assert result.status is Status.SKIPPED


def test_content_hash_covers_title_and_body_only():
    assert imports.content_sha256("t", "b") != imports.content_sha256("t", "b ")
    assert imports.content_sha256("ab", "") != imports.content_sha256("a", "b")


# ── 命令列 ──────────────────────────────────────────────────────────


def test_cli_end_to_end_and_doctor(fake, tmp_path):
    fake.all_notes_status = 500
    out = io.StringIO()
    export_dir = tmp_path / "export"
    assert mod.main(["export", "--out", str(export_dir)], getter=fake, stdout=out) == 0
    assert mod.main(["map", "--export", str(export_dir)], stdout=out) == 0
    db = tmp_path / "lore.db"
    args = [
        "import",
        "--export",
        str(export_dir),
        "--db",
        str(db),
        "--mapping",
        str(export_dir / mod.DEFAULT_MAPPING_FILE),
    ]
    assert mod.main(args, stdout=io.StringIO()) == 1  # 待人工確認 → 拒絕
    summary_buf = io.StringIO()
    assert mod.main([*args, "--allow-unreviewed"], stdout=summary_buf) == 0
    summary = json.loads(summary_buf.getvalue())
    assert summary["notes"]["inserted"] == 6
    assert summary["links"]["resolve_rate"] == pytest.approx(2 / 6, abs=1e-3)
    # stdout 摘要不含內容或標題
    assert "Alpha 決策" not in summary_buf.getvalue()
    assert (export_dir / mod.DEFAULT_REPORT_FILE).is_file()
    buf = io.StringIO()
    assert doctor_main(["--db", str(db), "--category", "import"], stdout=buf) == 0
    assert "import.on_reconcile" in buf.getvalue()

    est = io.StringIO()
    assert mod.main(["estimate", "--db", str(db)], stdout=est) == 0
    data = json.loads(est.getvalue())
    assert data["pending"] == {"summary": 6, "embedding": 6}
    assert data["summary"]["input_tokens_est"] > 0


# ── 控制字元：匯入不可拒收，清理並計數 ─────────────────────────────


def test_import_sanitizes_nul_and_reports(fake, tmp_path, conn):
    nul = chr(0)
    fake.note(
        "note:z",
        "有" + chr(1) + "控制",
        "前" + nul + "後" + nul,
        notebooks=("notebook:a",),
    )
    export, mapping = _prepare(fake, tmp_path)
    report = mod.run_import(conn, export, _reviewed(mapping))
    stored = get_note(conn, "github.com/u/alpha", "note:z")
    backslash = chr(92)
    assert stored.body == f"前{backslash}0後{backslash}0"
    assert stored.title == f"有{backslash}x01控制"
    assert report["sanitized"] == {
        "notes": [{"id": "note:z", "title": 1, "body": 2}],
        "chars": 3,
    }
    assert mod.report_summary(report)["sanitized"] == {"notes": ["note:z"], "chars": 3}
    assert _reconcile(conn).status is Status.PASS
    assert checks.control_chars(conn).status == "pass"
    # 清理在算雜湊之前：重跑冪等
    again = mod.run_import(conn, export, mapping)
    assert again["notes"]["unchanged"] == 7 and again["notes"]["inserted"] == 0


# ── 孤兒 note ───────────────────────────────────────────────────────

ORPHAN_CREATED = "2026-08-01T10:00:00.123456789Z"


def _tool_line(tool: str, tool_input: dict, *, cwd: str | None, ts: str) -> str:
    line = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "sess",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "無關"},
                {
                    "type": "tool_use",
                    "name": f"mcp__open-notebook__{tool}",
                    "input": tool_input,
                },
            ],
        },
    }
    if cwd is not None:
        line["cwd"] = cwd
    return json.dumps(line, ensure_ascii=False)


def _projects(tmp_path: Path) -> Path:
    """合成 transcript：create_note／update_note 的 tool_use 與干擾行。"""
    root = tmp_path / "projects"
    (root / "proj-a" / "sess" / "subagents").mkdir(parents=True)
    lines = [
        # 孤兒 A：標題比對（空白與大小寫不同也算）
        _tool_line(
            "create_note",
            {
                "title": "orphan  A",
                "content": "機密正文 A",
                "notebook_id": "notebook:a",
            },
            cwd="C:/w/alpha",
            ts="2026-08-01T10:00:01.000Z",
        ),
        # 孤兒 B：同標題建兩次、指向不同 vault，只有一次在時間窗內
        _tool_line(
            "create_note",
            {"title": "Orphan B", "content": "機密正文 B"},
            cwd="C:/w/beta",
            ts="2026-08-01T10:03:00.000Z",
        ),
        _tool_line(
            "create_note",
            {"title": "Orphan B", "content": "機密正文 B2"},
            cwd="C:/w/alpha",
            ts="2026-07-01T00:00:00.000Z",
        ),
        # 孤兒 D：cwd 算出的 key 不在 mapping
        _tool_line(
            "create_note",
            {"title": "Orphan D", "content": "機密正文 D"},
            cwd="C:/w/elsewhere",
            ts="2026-08-01T10:00:00.000Z",
        ),
        # tool_result（type=user）裡出現同標題：不可當證據
        json.dumps(
            {
                "type": "user",
                "cwd": "C:/w/alpha",
                "timestamp": "2026-08-01T10:00:02.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "mcp__open-notebook__create_note Orphan E",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        "{壞掉的 JSON open-notebook__create_note",
    ]
    (root / "proj-a" / "sess.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    # 孤兒 C：只有 update_note（note_id 精確比對）；訊息沒帶 cwd，退回同檔第一個 cwd
    sub = [
        json.dumps({"type": "user", "cwd": "C:/w/beta", "message": {"content": "x"}}),
        _tool_line(
            "update_note",
            {"note_id": "note:oc", "content": "機密正文 C"},
            cwd=None,
            ts="2026-08-02T00:00:00.000Z",
        ),
    ]
    (root / "proj-a" / "sess" / "subagents" / "agent-1.jsonl").write_text(
        "\n".join(sub) + "\n", encoding="utf-8"
    )
    return root


ORPHAN_KEYS = {
    "C:/w/alpha": "github.com/u/alpha",
    "C:/w/beta": "folder/beta",
    "C:/w/elsewhere": "folder/elsewhere",
}


def _orphan_inputs() -> list[on_orphans.OrphanInput]:
    return [
        on_orphans.OrphanInput("note:oa", "Orphan A", ORPHAN_CREATED),
        on_orphans.OrphanInput("note:ob", "Orphan B", ORPHAN_CREATED),
        on_orphans.OrphanInput("note:oc", "Orphan C（改過標題）", ORPHAN_CREATED),
        on_orphans.OrphanInput("note:od", "Orphan D", ORPHAN_CREATED),
        on_orphans.OrphanInput("note:oe", "Orphan E", ORPHAN_CREATED),
        on_orphans.OrphanInput("note:ox", "occupied", ORPHAN_CREATED),
    ]


def _orphan_map(fake, tmp_path):
    _, mapping = _prepare(fake, tmp_path)
    mapping = _reviewed(mapping)
    beta = next(e for e in mapping["notebooks"] if e["on_id"] == "notebook:b")
    beta["aliases"] = ["folder/beta-old"]
    orphan_map = on_orphans.build_orphan_map(
        _orphan_inputs(),
        mapping,
        [_projects(tmp_path)],
        exclude=["note:ox"],
        binder=ORPHAN_KEYS.__getitem__,
    )
    return mapping, orphan_map


def test_orphan_map_assigns_by_create_title_time_and_update_id(fake, tmp_path):
    _, orphan_map = _orphan_map(fake, tmp_path)
    by_id = {e["id"]: e for e in orphan_map["orphans"]}
    assert (by_id["note:oa"]["vault"], by_id["note:oa"]["basis"]) == (
        "github.com/u/alpha",
        on_orphans.BASIS_CREATE_TITLE,
    )
    assert by_id["note:oa"]["candidates"][0]["notebook_vault"] == "github.com/u/alpha"
    assert (by_id["note:ob"]["vault"], by_id["note:ob"]["basis"]) == (
        "folder/beta",
        on_orphans.BASIS_CREATE_TITLE_TIME,
    )
    assert (by_id["note:oc"]["vault"], by_id["note:oc"]["basis"]) == (
        "folder/beta",
        on_orphans.BASIS_UPDATE_ID,
    )
    assert by_id["note:oc"]["candidates"][0]["cwd"] == "C:/w/beta"
    assert by_id["note:od"]["needs_review"] and by_id["note:od"]["vault"] is None
    assert "folder/elsewhere" in by_id["note:od"]["review_reason"]
    # tool_result 裡的同名字串不算證據
    assert by_id["note:oe"]["needs_review"] and by_id["note:oe"]["candidates"] == []
    assert by_id["note:ox"]["skip"] and by_id["note:ox"]["basis"] == "excluded"
    # 只讀 tool_use input 的 title／note_id／notebook_id，不把正文帶進 mapping
    assert "機密正文" not in json.dumps(orphan_map, ensure_ascii=False)
    stats = on_orphans.orphan_map_stats(orphan_map)
    assert stats["auto_assigned"] == 3
    assert stats["per_vault"] == {"folder/beta": 2, "github.com/u/alpha": 1}
    assert [r["id"] for r in stats["needs_review"]] == ["note:od", "note:oe"]
    assert stats["excluded"] == ["note:ox"]


def test_orphan_map_flags_conflicting_create_without_time(fake, tmp_path):
    _, mapping = _prepare(fake, tmp_path)
    inputs = [on_orphans.OrphanInput("note:ob", "Orphan B", None)]
    orphan_map = on_orphans.build_orphan_map(
        inputs,
        _reviewed(mapping),
        [_projects(tmp_path)],
        binder=ORPHAN_KEYS.__getitem__,
    )
    [entry] = orphan_map["orphans"]
    assert entry["needs_review"] and "多個 vault" in entry["review_reason"]


def test_load_orphan_list_reads_tsv(tmp_path):
    path = tmp_path / "orphans.txt"
    path.write_text(
        "note:a\t2026-05-13T15:56:29\t2026-05-13T15:56:29\t190\thuman\tTrue\t\t標題 A\n"
        "note:b\t2026-05-14T00:00:00\t2026-05-14T00:00:00\t1\thuman\tFalse\tNULL\tB\n",
        encoding="utf-8",
    )
    rows = on_orphans.load_orphan_list(path)
    assert [(r.id, r.title, r.created) for r in rows] == [
        ("note:a", "標題 A", "2026-05-13T15:56:29"),
        ("note:b", "B", "2026-05-14T00:00:00"),
    ]


def _orphan_fake(fake):
    fake.all_notes_status = 500
    fake.note("note:oa", "Orphan A", "孤兒 A 內容", created=ORPHAN_CREATED)
    fake.note("note:ob", "Orphan B", "孤兒 B 內容", created=ORPHAN_CREATED)
    fake.note("note:oc", "Orphan C（改過標題）", "C", created=ORPHAN_CREATED)
    fake.note("note:od", "Orphan D", "D", created=ORPHAN_CREATED)
    fake.note("note:oe", "Orphan E", "E", created=ORPHAN_CREATED)
    fake.note("note:ox", "occupied", "x", created=ORPHAN_CREATED)

    def getter(url, headers, timeout):
        # 含 NUL 的那則：ON 逐筆端點也 500
        if url.endswith("/api/notes/note:oa"):
            return 500, b'{"detail":"Serialization error"}'
        return fake(url, headers, timeout)

    return getter


def _resolve_review(orphan_map, **vaults):
    for entry in orphan_map["orphans"]:
        if entry["id"] in vaults:
            entry.update(vault=vaults[entry["id"]], needs_review=False)
    return orphan_map


def _export_kwargs():
    return {
        "quote": mod._quote_id,
        "write_jsonl": mod._write_jsonl,
        "write_json": mod._write_json,
        "sha256_file": mod._sha256_file,
    }


def test_export_and_import_orphans_with_supplement(fake, tmp_path, conn):
    getter = _orphan_fake(fake)
    mapping, orphan_map = _orphan_map(fake, tmp_path)
    export_dir = tmp_path / "export"
    export = mod.load_export(export_dir)
    assert export.manifest["all_notes_endpoint"]["orphans_enumerable"] is False

    client = mod.OnClient(getter=getter)
    first = on_orphans.export_orphans(
        client.get, orphan_map, export_dir, **_export_kwargs()
    )
    assert first["unavailable"] == [{"id": "note:oa", "status": 500}]
    supplement_path = tmp_path / "supplement.jsonl"
    supplement_path.write_text(
        json.dumps(
            {
                "id": "note:oa",
                "title": "Orphan A",
                "content": "前" + chr(0) + "後",
                "created": ORPHAN_CREATED,
                "updated": ORPHAN_CREATED,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ids = {e["id"] for e in orphan_map["orphans"]}
    supplement = on_orphans.load_supplement(supplement_path, ids)
    manifest = on_orphans.export_orphans(
        client.get, orphan_map, export_dir, supplement=supplement, **_export_kwargs()
    )
    assert manifest["counts"] == {
        "records": 5,
        "rest": 4,
        "supplement": 1,
        "unavailable": 0,
    }
    records = on_orphans.load_orphan_records(export_dir, mod._sha256_file)
    orphans = mod.OrphanPlan(on_orphans.validate_orphan_map(orphan_map), records)

    with pytest.raises(mod.OnImportError, match="孤兒 mapping 有 2 項待人工確認"):
        mod.run_import(conn, export, mapping, orphans=orphans)

    _resolve_review(orphan_map, **{"note:od": "folder/beta-old"})
    # note:oe 仍未指定：--allow-unreviewed 下跳過並列報告
    report = mod.run_import(
        conn, export, mapping, orphans=orphans, allow_unreviewed=True
    )
    assert sorted(report["orphans"]["planned"]) == [
        "note:oa",
        "note:ob",
        "note:oc",
        "note:od",
    ]
    assert report["orphans"]["excluded"] == ["note:ox"]
    assert report["orphans"]["unassigned"] == ["note:oe"]
    assert report["skipped"]["orphan_excluded"] == 1
    assert report["per_vault"]["folder/beta"] == 1 + 3  # b1 + ob、oc、od（別名解析）
    oa = get_note(conn, "github.com/u/alpha", "note:oa")
    assert oa.body == "前" + chr(92) + "0後"
    assert report["sanitized"]["notes"] == [{"id": "note:oa", "title": 0, "body": 1}]
    assert _reconcile(conn).status is Status.PASS

    again = mod.run_import(
        conn, export, mapping, orphans=orphans, allow_unreviewed=True
    )
    assert again["notes"]["inserted"] == 0 and again["notes"]["unchanged"] == 10


def test_import_orphan_without_record_fails(fake, tmp_path, conn):
    _orphan_fake(fake)
    mapping, orphan_map = _orphan_map(fake, tmp_path)
    _resolve_review(orphan_map, **{"note:od": "folder/beta", "note:oe": "global"})
    export = mod.load_export(tmp_path / "export")
    orphans = mod.OrphanPlan(on_orphans.validate_orphan_map(orphan_map), [])
    with pytest.raises(mod.OnImportError, match="沒有內容紀錄"):
        mod.run_import(conn, export, mapping, orphans=orphans)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_import_orphan_vault_must_be_in_mapping(fake, tmp_path, conn):
    _orphan_fake(fake)
    mapping, orphan_map = _orphan_map(fake, tmp_path)
    _resolve_review(orphan_map, **{"note:od": "folder/nowhere", "note:oe": "global"})
    export = mod.load_export(tmp_path / "export")
    orphans = mod.OrphanPlan(on_orphans.validate_orphan_map(orphan_map), [])
    with pytest.raises(mod.OnImportError, match="不是 mapping 中的 vault"):
        mod.run_import(conn, export, mapping, orphans=orphans)


def test_cli_orphans_map_uses_real_binding_and_prints_only_stats(fake, tmp_path):
    _, mapping = _prepare(fake, tmp_path)
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(_reviewed(mapping)), encoding="utf-8")
    beta = tmp_path / "work" / "Beta"
    beta.mkdir(parents=True)
    root = tmp_path / "projects"
    root.mkdir()
    (root / "s.jsonl").write_text(
        _tool_line(
            "create_note",
            {"title": "孤兒一號", "content": "機密正文"},
            cwd=str(beta),
            ts="2026-08-01T10:00:00.000Z",
        )
        + "\n",
        encoding="utf-8",
    )
    orphan_list = tmp_path / "orphans.json"
    orphan_list.write_text(
        json.dumps(
            [
                {"id": "note:o1", "title": "孤兒一號", "created": ORPHAN_CREATED},
                {"id": "note:o2", "title": "測試", "created": ORPHAN_CREATED},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_path = tmp_path / "orphans-map.json"
    buf = io.StringIO()
    code = mod.main(
        [
            "orphans-map",
            "--orphans",
            str(orphan_list),
            "--mapping",
            str(mapping_path),
            "--out",
            str(out_path),
            "--projects-dir",
            str(root),
            "--exclude",
            "note:o2",
        ],
        stdout=buf,
    )
    assert code == 0
    stats = json.loads(buf.getvalue())
    assert stats["per_vault"] == {"folder/beta": 1}
    assert stats["excluded"] == ["note:o2"]
    assert "機密正文" not in buf.getvalue() and "孤兒一號" not in buf.getvalue()
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["orphans"][0]["vault"] == "folder/beta"
    assert "機密正文" not in out_path.read_text(encoding="utf-8")


def _codex_line(kind: str, payload: dict, ts: str = "2026-05-17T15:37:40.807Z") -> str:
    return json.dumps(
        {"timestamp": ts, "ordinal": 1, "type": kind, "payload": payload},
        ensure_ascii=False,
    )


def test_orphan_map_reads_codex_rollouts(fake, tmp_path):
    """Codex rollout：cwd 在 session_meta／turn_context，create_note 是
    namespace + name 的 function_call，arguments 是 JSON 字串。"""
    root = tmp_path / "codex" / "2026" / "05" / "17"
    root.mkdir(parents=True)
    create_args = {"title": "Codex 孤兒", "content": "機密正文", "notebook_id": "x"}
    lines = [
        _codex_line("session_meta", {"id": "s", "cwd": "C:/w/alpha"}),
        _codex_line("turn_context", {"cwd": "C:/w/beta", "model": "m"}),
        _codex_line(
            "response_item",
            {
                "type": "function_call",
                "namespace": "mcp__open_notebook__",
                "name": "create_note",
                "arguments": json.dumps(create_args, ensure_ascii=False),
                "call_id": "c1",
            },
        ),
        # 工具回傳（function_call_output）即使含同名字串也不看
        _codex_line(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": "mcp__open_notebook__create_note 另一則孤兒",
            },
        ),
    ]
    (root / "rollout-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, mapping = _prepare(fake, tmp_path)
    orphan_map = on_orphans.build_orphan_map(
        [
            on_orphans.OrphanInput("note:c1", "Codex 孤兒", "2026-05-17T15:37:41Z"),
            on_orphans.OrphanInput("note:c2", "另一則孤兒", None),
        ],
        _reviewed(mapping),
        [tmp_path / "codex"],
        binder=ORPHAN_KEYS.__getitem__,
    )
    first, second = orphan_map["orphans"]
    assert (first["vault"], first["basis"]) == ("folder/beta", "create_note_title")
    assert first["candidates"][0]["cwd"] == "C:/w/beta"  # 最近一次 turn_context
    assert second["needs_review"] and second["candidates"] == []
    assert "機密正文" not in json.dumps(orphan_map, ensure_ascii=False)
