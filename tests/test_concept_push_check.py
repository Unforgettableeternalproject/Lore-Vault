"""doctor `concept_push.lag`：服務端 concept 是否落後主機本地 concepts.json。

全部在 tmp_path 建 spike 資料目錄，不碰 ~/.lore-vault，也不連服務。
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main

AT = "2026-09-26T00:00:00+00:00"


def _home(
    tmp_path: Path,
    local: list[str],
    *,
    pushed: list[str] | None,
    record: dict | None = None,
) -> Path:
    home = tmp_path / "spike"
    home.mkdir()
    concepts = [{"id": i, "statement": f"s {i}"} for i in local]
    (home / "concepts.json").write_text(json.dumps(concepts), encoding="utf-8")
    state: dict = {}
    if pushed is not None:
        state["service_pushed_concept_ids"] = pushed
    if record is not None:
        state["concept_push"] = record
    (home / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    # concepts.json 早於推送時間（正常情況：推送讀完檔才寫紀錄）
    stamp = time.mktime(time.strptime("2025-01-01", "%Y-%m-%d"))
    os.utime(home / "concepts.json", (stamp, stamp))
    return home


def _ok_record(**extra) -> dict:
    return {"ok": True, "at": AT, "summary": "upsert 2、刪除 0", **extra}


def _lag(settings: dict):
    report = default_registry().run(
        DoctorContext(settings=settings), categories=["concept_push"]
    )
    (outcome,) = [o for o in report.outcomes if o.name == "concept_push.lag"]
    return outcome.result


def test_registered_under_its_own_category():
    names = {c.name: c.category for c in default_registry().checks}
    assert names["concept_push.lag"] == "concept_push"


def test_pass_when_every_local_id_was_pushed(tmp_path):
    home = _home(tmp_path, ["c-1", "c-2"], pushed=["c-1", "c-2"], record=_ok_record())
    result = _lag({"spike_home": str(home)})
    assert result.status is Status.PASS, result.summary
    assert result.counts == {
        "local": 2,
        "pushed": 2,
        "unpushed": 0,
        "pending_delete": 0,
    }


def test_unpushed_local_id_is_red(tmp_path):
    """管線新增了 concept 但沒推：服務端（與 PreToolUse 快照）落後。"""
    home = _home(
        tmp_path, ["c-1", "c-2", "c-9"], pushed=["c-1", "c-2"], record=_ok_record()
    )
    result = _lag({"spike_home": str(home)})
    assert result.status is Status.FAIL
    assert result.counts["unpushed"] == 1
    assert any("c-9" in d for d in result.details)


def test_pushed_id_gone_locally_is_red(tmp_path):
    """收斂刪掉的 concept 服務端還在：注入會拿到已刪除的記憶。"""
    home = _home(tmp_path, ["c-1"], pushed=["c-1", "c-2"], record=_ok_record())
    result = _lag({"spike_home": str(home)})
    assert result.status is Status.FAIL
    assert result.counts["pending_delete"] == 1


def test_last_push_failure_is_red_even_if_ids_match(tmp_path):
    record = {"ok": False, "at": AT, "summary": "ServiceRejected: HTTP 400"}
    home = _home(tmp_path, ["c-1"], pushed=["c-1"], record=record)
    result = _lag({"spike_home": str(home)})
    assert result.status is Status.FAIL
    assert any("HTTP 400" in d for d in result.details)


def test_never_pushed_warns(tmp_path):
    home = _home(tmp_path, ["c-1"], pushed=None)
    assert _lag({"spike_home": str(home)}).status is Status.WARN


def test_concepts_modified_after_push_warns(tmp_path):
    home = _home(tmp_path, ["c-1"], pushed=["c-1"], record=_ok_record())
    os.utime(home / "concepts.json")  # 現在 > AT
    result = _lag({"spike_home": str(home)})
    assert result.status is Status.WARN


def test_unreadable_concepts_is_red(tmp_path):
    home = _home(tmp_path, ["c-1"], pushed=["c-1"])
    (home / "concepts.json").write_text("{not json", encoding="utf-8")
    assert _lag({"spike_home": str(home)}).status is Status.FAIL


def test_skipped_without_settings_or_data(tmp_path):
    assert _lag({}).status is Status.SKIPPED
    assert _lag({"spike_home": str(tmp_path / "missing")}).status is Status.SKIPPED


def test_spool_dir_parent_is_the_fallback_home(tmp_path):
    home = _home(tmp_path, ["c-1", "c-2"], pushed=["c-1"], record=_ok_record())
    result = _lag({"spool_dir": str(home / "spool")})
    assert result.status is Status.FAIL


def test_cli_spike_home_flag(tmp_path):
    home = _home(tmp_path, ["c-1", "c-2"], pushed=["c-1"], record=_ok_record())
    out = io.StringIO()
    code = doctor_main(
        ["--json", "--category", "concept_push", "--spike-home", str(home)], stdout=out
    )
    data = json.loads(out.getvalue())
    (check,) = [c for c in data["checks"] if c["name"] == "concept_push.lag"]
    assert check["status"] == "fail"
    assert code == 1
