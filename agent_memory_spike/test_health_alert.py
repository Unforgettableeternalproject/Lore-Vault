"""健康告警的測試。

重點是**沒問題時要完全安靜**——會每次講話的告警，三天後就沒人在讀了，
那等於沒有告警。其餘各項是「多久沒動靜」的門檻。

執行：``python -m pytest agent_memory_spike/test_health_alert.py -q``
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import hook_health_alert as health  # noqa: E402


def _aged(path: Path, days: float) -> Path:
    """把檔案的 mtime 往前推。門檻判的是「多久沒動靜」，只能這樣造。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def work(tmp_path, monkeypatch):
    """一套「一切正常」的檔案佈局，各測試再往裡面弄壞一項。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "pipeline-20260824.log").write_text("ok", encoding="utf-8")
    state = tmp_path / "pipeline_state.json"
    state.write_text(json.dumps({"last_run": {"results": [
        {"stage": "collect", "ok": True, "summary": "補上 0 輪"},
        {"stage": "health", "ok": True, "summary": "未發現不一致"},
    ]}}), encoding="utf-8")
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    injections = tmp_path / "injections.jsonl"
    injections.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(health, "LOG_DIR", logs)
    monkeypatch.setattr(health, "STATE_PATH", state)
    monkeypatch.setattr(health, "EPISODE_DIR", episodes)
    monkeypatch.setattr(health, "INJECTION_LOG", injections)
    # D5 對帳看真實家目錄的舊位置，一律隔離到 tmp
    monkeypatch.setattr(health, "WORK_DIR", tmp_path)
    monkeypatch.setattr(health, "LEGACY_WORK_DIR", tmp_path / "legacy")
    return tmp_path


def test_silent_when_everything_is_fine(work):
    """一切正常就一個字都不能講。"""
    assert health.collect_alerts() == []
    assert health.run() is None


def test_pipeline_failure_is_reported(work):
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "collect", "ok": True, "summary": "補上 0 輪"},
        {"stage": "health", "ok": False, "summary": "[doctor] 發現 888 個問題："},
    ]}}), encoding="utf-8")
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "health" in alerts[0] and "888" in alerts[0]


def _daily_log(work, day: str, *runs: list[tuple[str, bool]]) -> None:
    """造一份 run_pipeline.ps1 格式的每日 log；每個 run 是 [(stage, ok), ...]。"""
    lines: list[str] = []
    for run in runs:
        lines.append(f"﻿=== pipeline start {day}T03:30:00+08:00 ===")
        for stage, ok in run:
            lines.append(f"\n[pipeline] === {stage} — 說明")
            lines.append(f"[pipeline] {'OK ' if ok else 'FAIL'} {stage} (12s): 摘要")
        lines.append("=== pipeline exit 1 ===")
    (work / "logs" / f"pipeline-{day.replace('-', '')}.log").write_text(
        "\n".join(lines), encoding="utf-8")


def _failed_last_run(work, stage: str) -> None:
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "collect", "ok": True, "summary": "補上 0 輪"},
        {"stage": stage, "ok": False, "summary": "受測失敗: 沒有輸出"},
    ]}}), encoding="utf-8")


CAL_FAIL = [("collect", True), ("calibrate", False)]
ALL_OK = [("collect", True), ("calibrate", True)]


def test_consecutive_failures_of_the_same_stage_are_counted(work):
    """同一階段連續卡住才是「卡死」：最新三份 log 都卡 calibrate，前一天成功。"""
    (work / "logs" / "pipeline-20260824.log").unlink()
    _daily_log(work, "2026-10-04", ALL_OK)
    _daily_log(work, "2026-10-05", CAL_FAIL)
    _daily_log(work, "2026-10-06", CAL_FAIL)
    # 同一天手動重跑過：只看最後一次
    _daily_log(work, "2026-10-07", ALL_OK, CAL_FAIL)
    _failed_last_run(work, "calibrate")
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "連續失敗 3 次" in alerts[0] and "calibrate" in alerts[0]


def test_single_failure_has_no_streak_note(work):
    """N=1 不標：單次失敗本來就會報，加註只是噪音。"""
    (work / "logs" / "pipeline-20260824.log").unlink()
    _daily_log(work, "2026-10-06", ALL_OK)
    _daily_log(work, "2026-10-07", CAL_FAIL)
    _failed_last_run(work, "calibrate")
    alerts = health.collect_alerts()
    assert len(alerts) == 1 and "連續失敗" not in alerts[0]


def test_streak_breaks_on_a_different_stage_and_skips_undecidable_logs(work):
    """別的階段失敗會打斷連續；沒有任何階段結果的 log（鎖被占用）不算也不打斷。"""
    (work / "logs" / "pipeline-20260824.log").unlink()
    _daily_log(work, "2026-10-04", CAL_FAIL)
    _daily_log(work, "2026-10-05", [("collect", True), ("health", False)])
    _daily_log(work, "2026-10-06", CAL_FAIL)
    _daily_log(work, "2026-10-07", [])
    _daily_log(work, "2026-10-08", CAL_FAIL)
    _failed_last_run(work, "calibrate")
    assert "連續失敗 2 次" in health.collect_alerts()[0]


def _with_push_record(work, record):
    state_path = work / "pipeline_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[health.CONCEPT_PUSH_KEY] = record
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def test_concept_push_failure_is_reported(work):
    """排程的推送失敗只留在 log 裡；服務端記憶凍結而快照年齡照樣綠，只有這裡看得到。"""
    _with_push_record(work, {"ok": False, "at": "2026-09-27T03:45:00+00:00",
                             "summary": "服務端拒收整批（batch_rejected）：2 筆 invalid（2 筆被拒）"})
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "concept 推送" in alerts[0] and "batch_rejected" in alerts[0]


def test_concept_push_success_is_silent(work):
    _with_push_record(work, {"ok": True, "at": "2026-09-27T03:45:00+00:00",
                             "summary": "upsert 3、刪除 0，套用 1/1 批"})
    assert health.collect_alerts() == []


def test_stale_schedule_is_reported(work):
    """管線失敗至少留得下 log；排程掛了連 log 都不會有，那更難察覺。"""
    _aged(work / "logs" / "pipeline-20260824.log", health.STALE_PIPELINE_DAYS + 1)
    assert any("排程可能停了" in a for a in health.collect_alerts())


def test_missing_log_dir_is_reported(work, monkeypatch):
    monkeypatch.setattr(health, "LOG_DIR", work / "nope")
    assert any("排程可能沒掛載" in a for a in health.collect_alerts())


def test_frozen_corpus_is_reported(work):
    """收料停掉是最根本的故障：下游全部還會用舊資料照常運作。"""
    _aged(work / "episodes", health.STALE_EPISODE_DAYS + 1)
    assert any("Stop hook" in a for a in health.collect_alerts())


def test_silent_injection_is_reported(work):
    _aged(work / "injections.jsonl", health.SILENT_INJECT_DAYS + 1)
    assert any("記憶被注入" in a for a in health.collect_alerts())


def test_injection_gap_within_threshold_is_silent(work):
    """注入本來就稀疏（約 18%/編輯輪），幾天沒有是正常的，不能喊。"""
    _aged(work / "injections.jsonl", health.SILENT_INJECT_DAYS - 1)
    assert health.collect_alerts() == []


def test_legacy_fallback_in_use_is_reported(work, monkeypatch):
    """D5：paths 還解析到舊位置代表資料沒搬，要講。"""
    monkeypatch.setattr(health, "LEGACY_WORK_DIR", work)
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "D5 過渡 fallback" in alerts[0]


def test_split_between_new_and_legacy_is_reported(work):
    """D5：新位置在用、舊位置又長出 episodes/，就是寫入分裂。"""
    (work / "legacy" / "episodes").mkdir(parents=True)
    alerts = health.collect_alerts()
    assert len(alerts) == 1
    assert "分裂" in alerts[0]


def test_legacy_dir_without_episodes_is_silent(work):
    """搬完留在舊位置的說明檔不算分裂。"""
    (work / "legacy").mkdir()
    (work / "legacy" / "README.txt").write_text("moved", encoding="utf-8")
    assert health.collect_alerts() == []


def test_broken_state_file_does_not_raise(work):
    """監控自己壞掉的代價是看不到警訊，不該是不能工作。"""
    (work / "pipeline_state.json").write_text("{ 壞掉的 JSON", encoding="utf-8")
    assert health.collect_alerts() == []


def test_output_shape_is_session_start_context(work):
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "distill", "ok": False, "summary": "零筆進帳"},
    ]}}), encoding="utf-8")
    out = health.run()
    assert "distill" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "distill" in out["hookSpecificOutput"]["additionalContext"]


def _run_main(monkeypatch, capsys) -> tuple[str, str]:
    """以 hook 模式跑 main()：stdin 餵 payload、無參數，回傳 (stdout, stderr)。"""
    monkeypatch.setattr(sys, "argv", ["hook_health_alert.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "SessionStart"}'))
    assert health.main() == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_main_is_silent_when_everything_is_fine(work, monkeypatch, capsys):
    """沒異常時 stdout 必須完全空白——連空 JSON 都不行。"""
    out, _ = _run_main(monkeypatch, capsys)
    assert out == ""


def test_main_alert_has_user_visible_system_message(work, monkeypatch, capsys):
    """additionalContext 只給模型看，被略過了 13 晚；必須另有 systemMessage 給人看。"""
    (work / "pipeline_state.json").write_text(json.dumps({"last_run": {"results": [
        {"stage": "health", "ok": False, "summary": "[doctor] 發現 888 個問題："},
    ]}}), encoding="utf-8")
    _aged(work / "episodes", health.STALE_EPISODE_DAYS + 1)
    out, _ = _run_main(monkeypatch, capsys)
    payload = json.loads(out)
    msg = payload["systemMessage"]
    assert msg.startswith("⚠️ coding agent 記憶層異常：")
    assert "health" in msg and "（共 2 項）" in msg
    assert "**" not in msg and "\n" not in msg
    # 模型那份細節仍要保留
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "888" in ctx and "Stop hook" in ctx


def test_user_summary_truncates_long_first_alert():
    msg = health.format_user_summary(["很長" * 100])
    assert "…" in msg and "共" not in msg


# ⚠️ `--check` 必須跳過 stdin：實測手動執行時沒有人會關 stdin，`read()` 直接卡死
# （踩過，卡滿兩分鐘）。這條沒有寫成測試——要測就得起 subprocess 並等它逾時，
# 為一行 if 付那個代價不划算。改動 main() 的 stdin 那段時記得手動跑一次 `--check`。
