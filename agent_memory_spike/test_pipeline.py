"""Phase 3 C 管線機制的測試。

裁決本身沒辦法測（那是一次真的 LLM 呼叫），但**圍繞裁決的那圈可以**，
而出事的通常是那圈：鎖沒清、JSON 抽錯、失敗了還往下跑。

執行：``python -m pytest agent_memory_spike/test_pipeline.py -q``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import pipeline  # noqa: E402
from pipeline import (  # noqa: E402
    LockBusy,
    acquire_lock,
    adjudicate_to_file,
    extract_json,
    release_lock,
    run_pipeline,
)


# --- 鎖 ---------------------------------------------------------------------

def test_lock_blocks_a_second_run(tmp_path):
    """兩條蒸餾疊在一起會重複寫入：watermark 是跑完才寫的，
    疊跑期間兩邊都看到「還沒做過」。"""
    lock = tmp_path / "pipeline.lock"
    acquire_lock(lock)
    with pytest.raises(LockBusy):
        acquire_lock(lock)
    release_lock(lock)
    acquire_lock(lock)  # 放掉之後可以再拿


def test_a_stale_lock_gets_taken_over(tmp_path, monkeypatch):
    """程序被 kill 時鎖不會自己消失，沒有接管機制管線會永遠停擺。"""
    lock = tmp_path / "pipeline.lock"
    acquire_lock(lock)
    old = time.time() - pipeline.LOCK_STALE_SECONDS - 60
    import os

    os.utime(lock, (old, old))
    acquire_lock(lock)  # 不該拋
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_is_released_even_when_a_stage_explodes(tmp_path, monkeypatch):
    """階段炸掉還握著鎖的話，下一次排程會被自己擋在門外。"""
    lock = tmp_path / "pipeline.lock"
    monkeypatch.setattr(pipeline, "LOCK_PATH", lock)
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")

    def boom(ctx):
        raise RuntimeError("炸了")

    monkeypatch.setattr(pipeline, "STAGES", [("boom", "測試用", boom)])
    acquire_lock(lock)
    release_lock(lock)
    assert run_pipeline(dry_run=False, max_groups=1, only=None) == 1


# --- JSON 抽取 --------------------------------------------------------------

def test_extract_json_from_a_fenced_block():
    reply = '我判完了，結果如下：\n\n```json\n{"verdicts": [{"id": "c-1"}]}\n```\n\n共 1 筆。'
    assert extract_json(reply) == {"verdicts": [{"id": "c-1"}]}


def test_extract_json_without_a_fence():
    """裁決者忘記加柵欄是常態，退路要救得回來。"""
    assert extract_json('結果：[{"id": "c-1"}] 以上。') == [{"id": "c-1"}]


def test_extract_json_returns_none_rather_than_garbage():
    """抽不出來要說抽不出來。**不能回一個空結構**——
    下游的 --ingest 收到零筆會看起來像正常跑完，那是靜默失敗。"""
    assert extract_json("我沒辦法完成這個任務。") is None


def test_adjudication_failure_writes_nothing(tmp_path, monkeypatch):
    """裁決回不出 JSON 時不該留下空檔案，理由同上。"""
    monkeypatch.setattr(pipeline, "adjudicate", lambda prompt, timeout=0: (True, "抱歉，我做不到"))
    target = tmp_path / "out" / "batch-00.json"
    ok, reason = adjudicate_to_file("...", target)
    assert not ok
    assert not target.exists()
    assert "沒有可解析的 JSON" in reason


def _fake_cli(monkeypatch, *, returncode: int, stdout: str = "", stderr: str = ""):
    import subprocess

    monkeypatch.setattr(pipeline, "claude_path", lambda: "claude")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a, returncode=returncode, stdout=stdout, stderr=stderr))


def test_empty_output_with_exit_zero_is_named(tmp_path, monkeypatch):
    """安全分類器撤回回答時 CLI exit 0 而 stdout/stderr 全空。
    以前的訊息是「沒有可解析的 JSON: 」接一段空白，夜間 log 無從追查。"""
    _fake_cli(monkeypatch, returncode=0, stdout="\n", stderr="")
    ok, reason = pipeline.adjudicate("...")
    assert not ok
    assert reason == pipeline.EMPTY_OUTPUT_REASON and "安全分類器" in reason

    target = tmp_path / "out.json"
    ok, reason = adjudicate_to_file("...", target)
    assert not ok and not target.exists()
    assert "沒有輸出" in reason and not reason.rstrip().endswith(":")


def test_nonzero_exit_carries_the_exit_code(monkeypatch):
    _fake_cli(monkeypatch, returncode=2)
    assert pipeline.adjudicate("...") == (False, "claude CLI 失敗（exit 2）且沒有任何輸出")
    _fake_cli(monkeypatch, returncode=1, stderr="boom")
    ok, reason = pipeline.adjudicate("...")
    assert not ok and "exit 1" in reason and "boom" in reason


def test_no_json_reason_never_has_an_empty_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "adjudicate", lambda prompt, timeout=0: (True, "   "))
    ok, reason = adjudicate_to_file("...", tmp_path / "x.json")
    assert not ok and reason == f"{pipeline.NO_JSON_REASON}: （回覆是空的）"


def test_adjudication_lands_the_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "adjudicate",
                        lambda prompt, timeout=0: (True, '```json\n[{"id": "c-1"}]\n```'))
    target = tmp_path / "out" / "batch-00.json"
    ok, summary = adjudicate_to_file("...", target)
    assert ok and "1 筆" in summary
    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": "c-1"}]


# --- 校準：受測拆批重試 -----------------------------------------------------

def _range_ids(prompt: str, flag: str) -> list[str] | None:
    """從 prompt 的 `--show-probes a-b`／`--show-judge a-b` 推回題目 id（c-000 起算）。"""
    import re

    match = re.search(flag + r" (\d+)-(\d+)", prompt)
    if not match:
        return None
    return [f"c-{i:03d}" for i in range(int(match.group(1)), int(match.group(2)) + 1)]


@pytest.fixture()
def calibrate_env(tmp_path, monkeypatch):
    """隔離 WORK_DIR、假的 calibrate 工具與裁決者；`script` 決定哪些受測範圍會失敗。"""
    monkeypatch.setattr(pipeline, "WORK_DIR", tmp_path)
    probes = [{"id": f"c-{i:03d}", "probe": f"q{i}", "statement": "s", "scope": None}
              for i in range(4)]
    (tmp_path / "probe_tasks.json").write_text(
        json.dumps({"probes": probes}), encoding="utf-8")
    env = {"calls": [], "fail": {}, "judge_calls": [], "judge_fail": {}, "judged": None,
           "ingested": None}

    def fake_run_tool(args, timeout=600):
        if "--ingest" in args:
            verdicts = json.loads(
                (tmp_path / "verdicts_auto" / "verdicts-00.json").read_text(encoding="utf-8"))
            # 同 calibrate.ingest：只認對得上 concept id、判定合法的記錄
            env["ingested"] = [v["id"] for v in verdicts if v.get("id") in ALL_IDS
                               and v.get("verdict") in pipeline.VERDICT_SCORES]
            return True, f"[calibrate] 更新 {len(env['ingested'])} 條 → x"
        return True, ""

    def fake_adjudicate(prompt, timeout=0):
        ids = _range_ids(prompt, "--show-probes")
        if ids is not None:
            env["calls"].append(f"{ids[0]}..{ids[-1]}")
            if (ids[0], ids[-1]) in env["fail"]:
                return env["fail"][(ids[0], ids[-1])]
            return True, "```json\n" + json.dumps(
                [{"id": i, "answer": f"答 {i}"} for i in ids]) + "\n```"
        # 判卷：同 show_judge，只判範圍內有作答的題目
        ids = _range_ids(prompt, "--show-judge")
        env["judge_calls"].append(f"{ids[0]}..{ids[-1]}")
        if (ids[0], ids[-1]) in env["judge_fail"]:
            return env["judge_fail"][(ids[0], ids[-1])]
        answers = json.loads(
            (tmp_path / "probe_out_auto" / "answers-00.json").read_text(encoding="utf-8"))
        answered = [a["id"] for a in answers if a["id"] in ids]
        env["judged"] = (env["judged"] or []) + answered
        return True, json.dumps([{"id": i, "verdict": "SILENT"} for i in answered])

    monkeypatch.setattr(pipeline, "run_tool", fake_run_tool)
    monkeypatch.setattr(pipeline, "adjudicate", fake_adjudicate)
    return env


ALL_IDS = {f"c-{i:03d}" for i in range(4)}


def _calibrate():
    return pipeline.stage_calibrate({"dry_run": False, "calibrate_max": 4})


def test_calibrate_retries_an_empty_answer_array(calibrate_env, capsys):
    """10/07 實例：受測回了合法的 `[]`，判卷材料一題都沒有，直到 ingest 才以
    「一條都沒對上 concepts」浮現。現在在受測當下就認出來並拆批重試。"""
    calibrate_env["fail"][("c-000", "c-003")] = (True, "```json\n[]\n```")
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["calls"] == ["c-000..c-003", "c-000..c-001", "c-002..c-003"]
    assert calibrate_env["ingested"] == sorted(ALL_IDS)
    assert pipeline.EMPTY_RESULT_REASON in capsys.readouterr().err


def test_calibrate_empty_answer_array_fails_with_a_diagnosable_reason(calibrate_env):
    for span in [("c-000", "c-003"), ("c-000", "c-001"), ("c-002", "c-003")]:
        calibrate_env["fail"][span] = (True, "[]")
    ok, summary = _calibrate()
    assert not ok and "受測失敗" in summary
    assert pipeline.EMPTY_RESULT_REASON in summary and "收到 0 筆" in summary
    assert calibrate_env["judge_calls"] == []  # 不再帶著零筆作答去判卷


def test_calibrate_judge_splits_an_empty_reply(calibrate_env, capsys):
    """判卷同樣會被分類器撤回：對半重試一次，合併後 ingest 吃得到全部判定。"""
    calibrate_env["judge_fail"][("c-000", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["judge_calls"] == ["c-000..c-003", "c-000..c-001", "c-002..c-003"]
    assert calibrate_env["ingested"] == sorted(ALL_IDS)
    assert "判卷 4 筆" in summary and "拆批重試" in summary
    assert "判卷整批 0-3 失敗" in capsys.readouterr().err


def test_calibrate_judge_with_rewritten_ids_names_both_sides(calibrate_env):
    """模型改寫 id 時拆批救不回來，但訊息要帶出兩邊的 id 樣本，一眼看得出是格式問題。"""
    rewritten = (True, json.dumps([{"id": "C000", "verdict": "SILENT"}]))
    for span in [("c-000", "c-003"), ("c-000", "c-001"), ("c-002", "c-003")]:
        calibrate_env["judge_fail"][span] = rewritten
    ok, summary = _calibrate()
    assert not ok and "判卷失敗" in summary
    assert "C000" in summary and "c-000" in summary
    assert len(calibrate_env["judge_calls"]) == 3


def test_calibrate_answer_records_without_answer_count_as_missing(calibrate_env, capsys):
    """受測回 `[{"id": "c-001"}]`：id 對上卻沒有 answer，不能算成功，要進拆批重試。
    拿掉欄位驗收，這條會在判卷前就以「沒有待處理的題目」紅掉。"""
    calibrate_env["fail"][("c-000", "c-003")] = (True, json.dumps([{"id": "c-001"}]))
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["calls"] == ["c-000..c-003", "c-000..c-001", "c-002..c-003"]
    assert calibrate_env["ingested"] == sorted(ALL_IDS)
    err = capsys.readouterr().err
    assert pipeline.EMPTY_RESULT_REASON in err and "1 筆 id 對上但欄位無效" in err


def test_calibrate_blank_answers_fail_with_a_diagnosable_reason(calibrate_env):
    blank = (True, json.dumps([{"id": "c-000", "answer": "  "}]))
    for span in [("c-000", "c-003"), ("c-000", "c-001"), ("c-002", "c-003")]:
        calibrate_env["fail"][span] = blank
    ok, summary = _calibrate()
    assert not ok and "受測失敗" in summary and "拆批重試後仍全部失敗" in summary
    assert calibrate_env["judge_calls"] == []


def test_calibrate_judge_with_illegal_verdicts_splits(calibrate_env, capsys):
    """判卷 verdict 不是合法判定（ingest 會全部略過）時視同缺漏，對半重試一次。"""
    illegal = (True, json.dumps([{"id": f"c-{i:03d}", "verdict": "KNEW"} for i in range(4)]))
    calibrate_env["judge_fail"][("c-000", "c-003")] = illegal
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["judge_calls"] == ["c-000..c-003", "c-000..c-001", "c-002..c-003"]
    assert calibrate_env["ingested"] == sorted(ALL_IDS)
    assert "4 筆 id 對上但欄位無效" in capsys.readouterr().err


def test_calibrate_judge_skips_the_half_without_answers(calibrate_env, capsys):
    """受測後半失敗時，判卷拆批不去判那一半（沒有作答，判了也是空的）。"""
    calibrate_env["fail"][("c-000", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    calibrate_env["fail"][("c-002", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    calibrate_env["judge_fail"][("c-000", "c-003")] = (True, "")
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["judge_calls"] == ["c-000..c-003", "c-000..c-001"]
    assert calibrate_env["ingested"] == ["c-000", "c-001"]
    assert "判卷拆批 2-3 沒有待處理的題目" in capsys.readouterr().err


def test_calibrate_splits_an_empty_reply_and_merges_the_halves(calibrate_env, capsys):
    """整批被安全分類器撤回（exit 0 空輸出）時對半拆開重試，合併成單批格式再判卷。
    拿掉拆批重試，這條會在「受測失敗」紅掉。"""
    calibrate_env["fail"][("c-000", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    ok, summary = _calibrate()
    assert ok, summary
    assert calibrate_env["calls"] == ["c-000..c-003", "c-000..c-001", "c-002..c-003"]
    # 合併後與單批同格式、id 全對得上，判卷看得到全部四題
    assert calibrate_env["judged"] == ["c-000", "c-001", "c-002", "c-003"]
    log = capsys.readouterr().err
    assert "拆成 0-1 / 2-3 重試一次" in log and "合併 4 筆" in log
    assert "拆批重試" in summary


def test_calibrate_split_retries_only_once(calibrate_env, capsys):
    """半批仍失敗不再遞迴；成功的那半照常判卷，失敗的範圍寫進 log 與摘要。"""
    calibrate_env["fail"][("c-000", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    calibrate_env["fail"][("c-002", "c-003")] = (True, "抱歉，我不能回答")
    ok, summary = _calibrate()
    assert ok, summary
    assert len(calibrate_env["calls"]) == 3
    assert calibrate_env["judged"] == ["c-000", "c-001"]
    assert "2-3 仍失敗" in summary
    assert "受測拆批 2-3 仍失敗" in capsys.readouterr().err


def test_calibrate_fails_when_both_halves_fail(calibrate_env):
    calibrate_env["fail"][("c-000", "c-003")] = (True, "")
    calibrate_env["fail"][("c-000", "c-001")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    calibrate_env["fail"][("c-002", "c-003")] = (False, pipeline.EMPTY_OUTPUT_REASON)
    ok, summary = _calibrate()
    assert not ok and "受測失敗" in summary and "拆批重試後仍全部失敗" in summary
    assert len(calibrate_env["calls"]) == 3


def test_calibrate_does_not_split_on_timeout(calibrate_env):
    """逾時拆小也不會好，照原樣失敗、不重試。"""
    calibrate_env["fail"][("c-000", "c-003")] = (False, "裁決逾時（1800s）")
    ok, summary = _calibrate()
    assert not ok and "逾時" in summary
    assert calibrate_env["calls"] == ["c-000..c-003"]


def test_calibrate_without_failure_runs_a_single_batch(calibrate_env):
    ok, summary = _calibrate()
    assert ok and "拆批" not in summary
    assert calibrate_env["calls"] == ["c-000..c-003"]


# --- 階段編排 ---------------------------------------------------------------

def test_a_failed_stage_stops_the_rest(tmp_path, monkeypatch):
    """語料壞掉時蒸餾只會蒸出錯的記憶，收斂沒跑完就校準則是白測。"""
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")
    ran: list[str] = []

    def ok_stage(ctx):
        ran.append("ok")
        return True, ""

    def bad_stage(ctx):
        ran.append("bad")
        return False, "壞了"

    def never(ctx):
        ran.append("never")
        return True, ""

    monkeypatch.setattr(pipeline, "STAGES", [
        ("a", "", ok_stage), ("b", "", bad_stage), ("c", "", never),
    ])
    assert run_pipeline(dry_run=False, max_groups=1, only=None) == 1
    assert ran == ["ok", "bad"]


def test_dry_run_leaves_no_state(tmp_path, monkeypatch):
    """dry-run 要能安全地在正式排程之前跑，不能污染上次執行的紀錄。"""
    state = tmp_path / "state.json"
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", state)
    monkeypatch.setattr(pipeline, "STAGES", [("a", "", lambda ctx: (True, ""))])
    run_pipeline(dry_run=True, max_groups=1, only=None)
    assert not state.exists()


def test_only_runs_the_requested_stage(tmp_path, monkeypatch):
    """某一階段的裁決失敗時，前面完成的部分不該重來——蒸餾特別貴。"""
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")
    ran: list[str] = []
    monkeypatch.setattr(pipeline, "STAGES", [
        ("a", "", lambda ctx: (ran.append("a"), (True, ""))[1]),
        ("b", "", lambda ctx: (ran.append("b"), (True, ""))[1]),
    ])
    run_pipeline(dry_run=False, max_groups=1, only="b")
    assert ran == ["b"]


def test_calibrate_max_is_separate_from_max_groups(tmp_path, monkeypatch):
    """放大校準上限不能連帶放大蒸餾／收斂的裁決批次（那邊 40 組就逾時）。"""
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "state.json")
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline, "STAGES", [
        ("a", "", lambda ctx: (seen.append((ctx["max_groups"], ctx["calibrate_max"])), (True, ""))[1]),
    ])
    run_pipeline(dry_run=False, max_groups=24, only=None, calibrate_max=72)
    run_pipeline(dry_run=False, max_groups=24, only=None)
    assert seen == [(24, 72), (24, 24)]


def test_credited_reads_the_actual_ledger_line():
    """管線層要自己驗收「這一輪真的進帳幾筆」。

    第一次自動實跑，蒸餾與判卷的回覆信封都與收回端不符，兩邊各收 0 筆，
    而 exit code 與摘要行都看起來像正常跑完。零筆進帳必須算失敗。
    """
    assert pipeline._credited("[distill] 已蒸餾組數 24 寫入 watermark") == 24
    assert pipeline._credited("[distill] 已蒸餾組數 0 寫入 watermark") == 0
    assert pipeline._credited("[calibrate] 更新 12 條 → x", r"更新 (\d+) 條") == 12
    # 找不到那行 = 版本不合，寧可誤報失敗也不靜默放行
    assert pipeline._credited("完全無關的輸出") == -1


# --- 切換：直譯器與排程腳本 -------------------------------------------------

def test_tool_python_is_repo_relative_venv():
    """裁決者的 allowlist 認字面：必須是相對 repo 根的路徑，且不再依賴 U.E.P env。"""
    tool = pipeline.TOOL_PYTHON
    assert not Path(tool).is_absolute()
    assert ".." not in Path(tool).parts
    assert "U.E.P" not in tool
    assert tool == ".venv/Scripts/python.exe"


def test_run_pipeline_script_keeps_its_two_traps():
    """排程腳本：UTF-8 含 BOM（PS 5.1 否則吞掉中文註解後的行）、
    Python 輸出走 cmd /c 重導而非 *>>，直譯器用本 repo 的 .venv。"""
    raw = (Path(__file__).parent / "run_pipeline.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "U.E.P-s-Core" not in text
    assert r'$python = Join-Path $repo ".venv\Scripts\python.exe"' in text
    assert "& cmd /c" in text
    assert not any("*>>" in line for line in text.splitlines()
                   if not line.lstrip().startswith("#"))


def test_run_pipeline_script_pushes_concepts_only_after_a_successful_run():
    """--run 成功才推；推送也走 cmd /c 進同一份 log，失敗反映在腳本 exit code。"""
    text = (Path(__file__).parent / "run_pipeline.ps1").read_bytes().decode("utf-8-sig")
    code_lines = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    run_at = next(i for i, line in enumerate(code_lines) if "--run" in line)
    gate_at = next(i for i, line in enumerate(code_lines) if line.startswith("if ($code -eq 0)"))
    push_at = next(i for i, line in enumerate(code_lines) if "--push-concepts" in line)
    assert run_at < gate_at < push_at
    push_line = code_lines[push_at]
    assert push_line.startswith("& cmd /c") and '>> `"$log`" 2>&1' in push_line
    assert "if ($pushCode -ne 0) { $code = $pushCode }" in code_lines
    assert code_lines[-1] == "exit $code"
