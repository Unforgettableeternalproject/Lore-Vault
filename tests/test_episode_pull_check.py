"""doctor `episode_pull.status`：管線的蒸餾語料是否涵蓋服務端全部機器的 episode（D13）。

全部在 tmp_path 建 spike 資料目錄，不碰 ~/.lore-vault，也不連服務。
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main

NOW = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
AT = "2026-09-27T03:31:00+00:00"


def _home(tmp_path: Path, record: dict | None) -> Path:
    home = tmp_path / "spike"
    home.mkdir()
    state: dict = {}
    if record is not None:
        state["episode_pull"] = record
    (home / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    return home


def _ok(**extra) -> dict:
    record = {
        "ok": True,
        "mode": "service",
        "at": AT,
        "last_ok_at": AT,
        "service_total": 10,
        "legacy": False,
        "counts": {"cached": 10, "merged": 12, "server_missing": 0},
    }
    record.update(extra)
    return record


def _status(home: Path, **settings):
    report = default_registry().run(
        DoctorContext(settings={"spike_home": str(home), "now": NOW, **settings}),
        categories=["episode_pull"],
    )
    (outcome,) = [o for o in report.outcomes if o.name == "episode_pull.status"]
    return outcome.result


def test_registered_under_its_own_category():
    names = {c.name: c.category for c in default_registry().checks}
    assert names["episode_pull.status"] == "episode_pull"


def test_pass_after_a_clean_service_pull(tmp_path):
    assert _status(_home(tmp_path, _ok())).status is Status.PASS


def test_never_pulled_warns(tmp_path):
    assert _status(_home(tmp_path, None)).status is Status.WARN


def test_missing_state_file_is_skipped(tmp_path):
    home = tmp_path / "spike"
    home.mkdir()
    assert _status(home).status is Status.SKIPPED


def test_cache_count_mismatch_fails(tmp_path):
    record = _ok(counts={"cached": 9, "merged": 12, "server_missing": 0})
    result = _status(_home(tmp_path, record))
    assert result.status is Status.FAIL and "不符" in result.summary


def test_server_missing_fails_even_when_pull_succeeded(tmp_path):
    record = _ok(
        counts={"cached": 12, "merged": 12, "server_missing": 2},
        server_missing_sample=[["s", "p", 0]],
    )
    result = _status(_home(tmp_path, record))
    assert result.status is Status.FAIL and "缺少 2 筆" in result.summary


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # 剛降級一次：warn
        (
            {"mode": "local_fallback", "error_kind": "unavailable", "last_ok_at": AT},
            Status.WARN,
        ),
        # 降級且已多天沒成功：fail
        (
            {
                "mode": "local_fallback",
                "error_kind": "unavailable",
                "last_ok_at": "2026-09-20T03:30:00+00:00",
            },
            Status.FAIL,
        ),
        # 對帳錯誤造成的降級：fail
        (
            {"mode": "local_fallback", "error_kind": "consistency", "last_ok_at": AT},
            Status.FAIL,
        ),
        ({"mode": "local_forced"}, Status.WARN),
        ({"mode": "failed", "error_kind": "unavailable"}, Status.FAIL),
        ({"mode": "???"}, Status.FAIL),
    ],
)
def test_degraded_modes(tmp_path, record, expected):
    full = {"ok": record["mode"] == "local_forced", "at": AT, "counts": {}, **record}
    assert _status(_home(tmp_path, full)).status is expected


def test_legacy_server_warns(tmp_path):
    record = _ok(legacy=True, service_total=None)
    assert _status(_home(tmp_path, record)).status is Status.WARN


def test_cli_spike_home_flag(tmp_path):
    home = _home(tmp_path, _ok(counts={"cached": 9, "server_missing": 0}))
    out = io.StringIO()
    code = doctor_main(
        ["--json", "--category", "episode_pull", "--spike-home", str(home)], stdout=out
    )
    data = json.loads(out.getvalue())
    (check,) = [c for c in data["checks"] if c["name"] == "episode_pull.status"]
    assert check["status"] == "fail"
    assert code == 1
