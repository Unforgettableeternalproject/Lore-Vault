"""D13：執行期設定覆寫的儲存、稽核與 doctor 對帳（拿掉保護會紅）。"""

from __future__ import annotations

import io
import json

import pytest

from lore_vault.config import Config, EpisodesConfig
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main
from lore_vault.runtime_settings import SPECS, InvalidSetting, apply_overrides
from lore_vault.storage import settings_store as store
from lore_vault.storage.db import connect
from lore_vault.storage.migrate import MIGRATIONS, migrate

NOW = "2026-09-27T00:00:00.000Z"


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lore.db")
    yield connection
    connection.close()


def _doctor(conn, **settings) -> dict:
    report = default_registry().run(
        DoctorContext(settings=settings, resources={"db": conn}),
        categories=["settings", "episodes"],
    )
    return {o.name: o.result for o in report.outcomes}


def _change(conn, **kw):
    return store.change(
        conn, Config(), principal="UEPBernie", display="Xavier (Bernie)", now=NOW, **kw
    )


def test_catalog_matches_config_and_validates_strictly():
    config = Config()
    for spec in SPECS:
        # 每個鍵都對得上 Config 欄位，預設值本身合法（在範圍內）
        section = getattr(config, spec.section)
        assert hasattr(section, spec.name)
    with pytest.raises(InvalidSetting):
        apply_overrides(config, {"episodes.ingest": "true"})
    with pytest.raises(InvalidSetting):
        apply_overrides(config, {"api.enrich_worker": False})
    applied = apply_overrides(config, {"episodes.ingest": True, "ask.enabled": False})
    assert applied.episodes.ingest is True and applied.ask.enabled is False
    # 原 Config 不動（frozen dataclass）
    assert config.episodes.ingest is False


def test_change_writes_override_and_audit_in_one_transaction(conn):
    entries = _change(conn, set_values={"episodes.ingest": True})
    assert [(e.key, e.action, e.old_value, e.new_value) for e in entries] == [
        ("episodes.ingest", "set", False, True)
    ]
    assert store.read_override(conn, "episodes.ingest") is True
    with pytest.raises(store.InvalidSettings) as info:
        _change(
            conn,
            set_values={"ask.enabled": False, "ask.snippet_max_chars": 10},
        )
    assert [e.key for e in info.value.errors] == ["ask.snippet_max_chars"]
    # 整批不寫：ask.enabled 沒有變
    assert store.read_override(conn, "ask.enabled") is None
    assert len(store.audit_log(conn)) == 1
    entries = _change(conn, reset_keys=["episodes.ingest"])
    assert [(e.action, e.old_value, e.new_value) for e in entries] == [
        ("reset", True, False)
    ]
    assert store.read_override(conn, "episodes.ingest") is None


def test_overrides_validity_turns_red_on_bad_rows(conn):
    _change(conn, set_values={"ask.enabled": False})
    assert _doctor(conn)["settings.overrides"].status is Status.PASS
    conn.execute(
        "INSERT INTO settings_overrides (key, value, updated, updated_by) "
        "VALUES ('worker.batch_size', '5', ?, 'x')",
        (NOW,),
    )
    conn.execute(
        "UPDATE settings_overrides SET value = '99999999' WHERE key = 'ask.enabled'"
    )
    result = _doctor(conn)["settings.overrides"]
    assert result.status is Status.FAIL
    assert result.counts["invalid"] == 2
    assert any("worker.batch_size" in d for d in result.details)
    # 執行期略過不合法的列
    values, _, invalid = store.effective_overrides(conn)
    assert values == {} and len(invalid) == 2


def test_audit_agreement_turns_red_on_unaudited_writes(conn):
    _change(conn, set_values={"ask.snippet_max_chars": 3000})
    assert _doctor(conn)["settings.audit_agreement"].status is Status.PASS
    # 繞過 change() 改值：沒有留下誰改的紀錄
    conn.execute(
        "UPDATE settings_overrides SET value = '4000' "
        "WHERE key = 'ask.snippet_max_chars'"
    )
    assert _doctor(conn)["settings.audit_agreement"].status is Status.FAIL
    conn.execute("DELETE FROM settings_overrides")
    # 稽核最後一筆是 set，覆寫卻不見了
    result = _doctor(conn)["settings.audit_agreement"]
    assert result.status is Status.FAIL
    _change(conn, set_values={"ask.snippet_max_chars": 2000})
    _change(conn, reset_keys=["ask.snippet_max_chars"])
    assert _doctor(conn)["settings.audit_agreement"].status is Status.PASS


def test_settings_checks_skip_before_v15(tmp_path):
    import sqlite3

    raw = sqlite3.connect(tmp_path / "v14.db", isolation_level=None)
    raw.row_factory = sqlite3.Row
    try:
        migrate(raw, migrations=MIGRATIONS[:14])
        results = _doctor(raw)
        assert results["settings.overrides"].status is Status.SKIPPED
        assert results["settings.audit_agreement"].status is Status.SKIPPED
    finally:
        raw.close()


def test_ingest_recency_skipped_by_setting_or_db_override(conn):
    # 服務依執行期設定傳入
    assert (
        _doctor(conn, episodes_ingest=False)["episodes.ingest_recency"].status
        is Status.SKIPPED
    )
    assert (
        _doctor(conn, episodes_ingest=True)["episodes.ingest_recency"].status
        is Status.WARN
    )
    # doctor CLI 沒有設定值：沒有覆寫時照常檢查，DB 覆寫為關閉時 skipped
    assert _doctor(conn)["episodes.ingest_recency"].status is Status.WARN
    _change(conn, set_values={"episodes.ingest": False})
    assert _doctor(conn)["episodes.ingest_recency"].status is Status.SKIPPED
    assert EpisodesConfig().ingest is False


def test_doctor_cli_reads_db_override(tmp_path):
    db = tmp_path / "lore.db"
    conn = connect(db)
    try:
        _change(conn, set_values={"episodes.ingest": False})
    finally:
        conn.close()
    out = io.StringIO()
    code = doctor_main(
        ["--db", str(db), "--json", "--category", "episodes", "--category", "settings"],
        stdout=out,
    )
    report = json.loads(out.getvalue())
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["episodes.ingest_recency"] == "skipped"
    assert statuses["settings.overrides"] == "pass"
    assert code == 0
