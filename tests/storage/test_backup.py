"""T-27：`VACUUM INTO` 備份、保留份數、半檔保護與 doctor `backup.recent` 對帳。"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main
from lore_vault.storage import backup as backup_mod
from lore_vault.storage.backup import (
    SIDECAR_NAME,
    BackupError,
    backup_database,
    backup_freshness,
    main,
    verify_backup,
)
from lore_vault.storage.errors import StorageError
from lore_vault.storage.migrate import SCHEMA_VERSION

T0 = datetime(2026, 9, 26, 3, 0, 0, tzinfo=UTC)
HOUR = 3600.0


@pytest.fixture
def populated(conn, add_vault, add_note, db_path):
    add_vault("repo-a")
    add_note("repo-a", "n-1", "標題", "內容")
    return db_path


def _doctor(backup_dir, now, **settings):
    report = default_registry().run(
        DoctorContext(settings={"backup_dir": backup_dir, "now": now, **settings}),
        categories=["backup"],
    )
    (outcome,) = report.outcomes
    return outcome.result


def test_backup_is_independently_openable(populated, tmp_path):
    dest = tmp_path / "backups"
    record, pruned = backup_database(populated, dest, keep=3, now=T0)

    assert record.file == "lore-20260926T030000000Z.db"
    assert record.created == "2026-09-26T03:00:00.000Z"
    assert record.schema_version == SCHEMA_VERSION
    assert pruned == []
    copy = sqlite3.connect(
        f"{(dest / record.file).resolve().as_uri()}?mode=ro", uri=True
    )
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT title FROM notes").fetchone()[0] == "標題"
    finally:
        copy.close()
    sidecar = json.loads((dest / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["file"] == record.file and sidecar["integrity"] == "ok"
    # 只有正式檔與 sidecar，沒有暫存殘留
    assert sorted(p.name for p in dest.iterdir()) == [SIDECAR_NAME, record.file]


def test_keeps_only_latest_n(populated, tmp_path):
    dest = tmp_path / "backups"
    (dest).mkdir()
    unrelated = dest / "notes.txt"
    unrelated.write_text("x", encoding="utf-8")
    names = []
    for i in range(4):
        record, _ = backup_database(
            populated, dest, keep=2, now=T0 + timedelta(hours=i)
        )
        names.append(record.file)
    remaining = sorted(p.name for p in dest.glob("lore-*.db"))
    assert remaining == names[-2:]
    assert unrelated.exists()  # 不符合備份檔名的檔案不動


def test_verification_failure_leaves_no_partial(populated, tmp_path, monkeypatch):
    dest = tmp_path / "backups"

    def broken(path, *, expected_version):
        assert path.exists()  # 暫存檔確實寫出了
        raise BackupError("模擬驗證失敗")

    monkeypatch.setattr(backup_mod, "verify_backup", broken)
    with pytest.raises(BackupError):
        backup_database(populated, dest, keep=3, now=T0)
    assert list(dest.iterdir()) == []  # 沒有半檔、也沒有 sidecar


def test_verify_rejects_corrupt_and_version_mismatch(populated, tmp_path):
    record, _ = backup_database(populated, tmp_path / "b", keep=1, now=T0)
    good = tmp_path / "b" / record.file
    with pytest.raises(BackupError, match="schema 版本"):
        verify_backup(good, expected_version=SCHEMA_VERSION + 1)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(BackupError):
        verify_backup(bad, expected_version=SCHEMA_VERSION)


def test_missing_source_fails_without_residue(tmp_path):
    dest = tmp_path / "backups"
    with pytest.raises(StorageError):
        backup_database(tmp_path / "nope.db", dest, keep=1, now=T0)
    assert list(dest.iterdir()) == []


def test_source_is_not_written(populated, tmp_path):
    before = populated.stat().st_mtime_ns
    backup_database(populated, tmp_path / "b", keep=1, now=T0)
    assert populated.stat().st_mtime_ns == before


# ── doctor 對帳：備份沒跑時要紅 ──


def test_doctor_fails_when_never_backed_up(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    result = _doctor(dest, T0)
    assert result.status is Status.FAIL
    assert "從未備份" in result.summary


def test_doctor_red_when_backup_stale_green_when_fresh(populated, tmp_path):
    dest = tmp_path / "backups"
    backup_database(populated, dest, keep=3, now=T0)

    assert _doctor(dest, T0 + timedelta(hours=1)).status is Status.PASS
    stale = _doctor(dest, T0 + timedelta(hours=27))
    assert stale.status is Status.FAIL
    assert "超過門檻" in stale.summary
    # 門檻是設定項
    relaxed = _doctor(dest, T0 + timedelta(hours=27), backup_max_age_hours=48)
    assert relaxed.status is Status.PASS


def test_doctor_fails_when_referenced_file_deleted(populated, tmp_path):
    dest = tmp_path / "backups"
    record, _ = backup_database(populated, dest, keep=3, now=T0)
    (dest / record.file).unlink()
    result = _doctor(dest, T0 + timedelta(hours=1))
    assert result.status is Status.FAIL
    assert "不存在" in result.summary


def test_doctor_fails_on_corrupt_sidecar(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / SIDECAR_NAME).write_text("{", encoding="utf-8")
    assert _doctor(dest, T0).status is Status.FAIL
    (dest / SIDECAR_NAME).write_text(
        json.dumps({"created": "2026-09-26T03:00:00.000Z", "file": "../lore.db"}),
        encoding="utf-8",
    )
    assert _doctor(dest, T0).status is Status.FAIL


def test_doctor_skips_without_backup_dir():
    report = default_registry().run(DoctorContext(), categories=["backup"])
    assert report.outcomes[0].result.status is Status.SKIPPED


def test_freshness_counts(populated, tmp_path):
    dest = tmp_path / "b"
    backup_database(populated, dest, keep=1, now=T0)
    rec = backup_freshness(dest, now=T0 + timedelta(hours=2), max_age_seconds=HOUR)
    assert rec.status == "fail"
    assert rec.counts == {"age_seconds": 7200, "max_age_seconds": 3600}


# ── CLI ──


def test_cli_backup_then_doctor(populated, tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_VAULT_CONFIG", raising=False)
    dest = tmp_path / "cli-backups"
    out = io.StringIO()
    assert (
        main(["--db", str(populated), "--dest", str(dest), "--keep", "2"], stdout=out)
        == 0
    )
    payload = json.loads(out.getvalue())
    assert (dest / payload["file"]).is_file()

    buf = io.StringIO()
    code = doctor_main(
        ["--category", "backup", "--backup-dir", str(dest), "--json"], stdout=buf
    )
    assert code == 0
    # 把最近一次備份推回 30 小時前（等同排程停擺）→ 預設門檻 26 小時下變紅
    sidecar = dest / SIDECAR_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    old = datetime.now(UTC) - timedelta(hours=30)
    data["created"] = old.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    buf = io.StringIO()
    code = doctor_main(["--category", "backup", "--backup-dir", str(dest)], stdout=buf)
    assert code == 1
    assert "超過門檻" in buf.getvalue()


def test_cli_requires_paths(tmp_path, monkeypatch):
    for name in (
        "LORE_VAULT_CONFIG",
        "LORE_VAULT_DATABASE_PATH",
        "LORE_VAULT_BACKUP_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert main(["--dest", str(tmp_path)]) == 2
    assert main(["--db", str(tmp_path / "x.db")]) == 2


def test_cli_reports_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_VAULT_CONFIG", raising=False)
    code = main(["--db", str(tmp_path / "missing.db"), "--dest", str(tmp_path / "b")])
    assert code == 1
