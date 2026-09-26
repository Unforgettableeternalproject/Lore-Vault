"""T-14：doctor 框架。

重點：
- 空 doctor 不報錯、exit 0
- 檢查拋例外 / 回傳型別錯 → 記成 fail，其他項照跑，exit 非 0
- 缺資源 → skipped 附原因
- 內建 `hooks.stdlib_only` 經框架執行時，違規目錄會紅
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lore_vault.doctor import (
    EXIT_FAIL,
    EXIT_OK,
    Check,
    CheckResult,
    DoctorContext,
    Registry,
    Status,
    default_registry,
)
from lore_vault.doctor import framework as fw
from lore_vault.doctor.command import main

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _run_cli(argv, **kwargs):
    buf = io.StringIO()
    code = main(argv, stdout=buf, **kwargs)
    return code, buf.getvalue()


# ── 空 doctor ───────────────────────────────────────────────────────


def test_empty_registry_runs_clean():
    report = Registry().run()
    assert report.ok and report.exit_code == EXIT_OK
    assert report.to_dict() == {
        "ok": True,
        "exit_code": 0,
        "summary": {"total": 0, "pass": 0, "fail": 0, "warn": 0, "skipped": 0},
        "checks": [],
    }
    code, text = _run_cli([], registry=Registry())
    assert code == EXIT_OK and "沒有註冊任何檢查項" in text
    code, text = _run_cli(["--json"], registry=Registry())
    assert code == EXIT_OK and json.loads(text)["summary"]["total"] == 0


# ── 結果與報告 ─────────────────────────────────────────────────────


def _mixed_registry() -> Registry:
    reg = Registry()

    @reg.register("demo.pass", "demo")
    def _p(ctx):
        return CheckResult.ok("好", counts={"rows": 3})

    @reg.register("demo.warn", "demo")
    def _w(ctx):
        return CheckResult.warn("接近門檻", details=["a", "b"])

    @reg.register("db.needs_db", "db")
    def _s(ctx):
        ctx.require("db")
        return CheckResult.ok()

    return reg


def test_structured_report_and_exit_code_without_fail():
    report = _mixed_registry().run(DoctorContext())
    data = report.to_dict()
    assert data["ok"] is True and data["exit_code"] == EXIT_OK
    assert data["summary"] == {
        "total": 3,
        "pass": 1,
        "fail": 0,
        "warn": 1,
        "skipped": 1,
    }
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["demo.pass"]["counts"] == {"rows": 3}
    assert by_name["demo.warn"]["details"] == ["a", "b"]
    assert by_name["db.needs_db"]["status"] == "skipped"
    assert "db" in by_name["db.needs_db"]["summary"]
    json.dumps(data)  # 必須可序列化


def test_context_resource_is_passed_to_check():
    report = _mixed_registry().run(DoctorContext(resources={"db": object()}))
    assert report.count(Status.SKIPPED) == 0
    assert report.count(Status.PASS) == 2


def test_category_filter():
    report = _mixed_registry().run(categories=["db"])
    assert [o.name for o in report.outcomes] == ["db.needs_db"]


def test_unknown_category_is_usage_error():
    """打錯分類名不可篩成 0 項後當成通過。"""
    with pytest.raises(SystemExit) as exc:
        _run_cli(["--category", "hook"], registry=_mixed_registry())
    assert exc.value.code == 2


def test_fail_sets_nonzero_exit():
    reg = Registry([Check("x.bad", "x", lambda ctx: CheckResult.fail("不一致"))])
    report = reg.run()
    assert not report.ok and report.exit_code == EXIT_FAIL
    code, text = _run_cli([], registry=reg)
    assert code == EXIT_FAIL and "[FAIL] x.bad" in text


# ── 對帳本身壞掉不可被當成通過 ────────────────────────────────────


def _boom(ctx):
    raise RuntimeError("連線中斷")


def test_raising_check_is_recorded_as_fail_and_others_still_run():
    reg = _mixed_registry()
    reg.add(Check("demo.boom", "demo", _boom))
    reg.add(Check("demo.after", "demo", lambda ctx: CheckResult.ok()))
    report = reg.run()
    by_name = {o.name: o.result for o in report.outcomes}
    assert by_name["demo.boom"].status is Status.FAIL
    assert "RuntimeError" in by_name["demo.boom"].summary
    assert any("連線中斷" in line for line in by_name["demo.boom"].details)
    # 例外之後的檢查項照跑
    assert by_name["demo.after"].status is Status.PASS
    assert report.exit_code == EXIT_FAIL
    code, text = _run_cli(["--json"], registry=reg)
    assert code == EXIT_FAIL
    assert json.loads(text)["summary"]["fail"] == 1


@pytest.mark.parametrize("bad", [None, True, {"status": "pass"}])
def test_wrong_return_type_is_fail(bad):
    reg = Registry([Check("x.wrong", "x", lambda ctx: bad)])
    report = reg.run()
    assert report.outcomes[0].result.status is Status.FAIL
    assert report.exit_code == EXIT_FAIL


def test_exception_guard_is_load_bearing(monkeypatch):
    """拿掉 `_run_one` 的保護時，拋例外的檢查會讓整個 doctor 崩潰。"""
    monkeypatch.setattr(fw, "_run_one", lambda check, ctx: check.func(ctx))
    reg = Registry([Check("demo.boom", "demo", _boom)])
    with pytest.raises(RuntimeError):
        reg.run()


# ── 註冊與結果型別的防呆 ───────────────────────────────────────────


def test_duplicate_name_rejected():
    reg = Registry([Check("a.b", "a", lambda ctx: CheckResult.ok())])
    with pytest.raises(ValueError, match="重複"):
        reg.add(Check("a.b", "a", lambda ctx: CheckResult.ok()))


def test_result_validation():
    with pytest.raises(ValueError, match="summary"):
        CheckResult(Status.FAIL)
    with pytest.raises(ValueError, match="summary"):
        CheckResult.skipped("  ")
    with pytest.raises(TypeError):
        CheckResult.ok(details="單一字串")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CheckResult.ok(counts={"n": 1.5})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        CheckResult("maybe")  # type: ignore[arg-type]
    assert CheckResult("warn", "x").status is Status.WARN  # type: ignore[arg-type]


def test_registries_are_independent():
    """沒有模組級單例：各自建立的 registry 互不影響。"""
    a, b = default_registry(), default_registry()
    a.add(Check("extra.one", "extra", lambda ctx: CheckResult.ok()))
    assert len(b) == len(a) - 1


# ── 內建檢查項 ─────────────────────────────────────────────────────


def test_hook_imports_registered_and_passes_on_real_hooks():
    reg = default_registry()
    assert "hooks.stdlib_only" in [c.name for c in reg.checks]
    report = reg.run(categories=["hooks"])
    result = report.outcomes[0].result
    assert result.status is Status.PASS, result.details
    assert result.counts["scanned"] >= 2 and result.counts["violations"] == 0


def test_hook_imports_check_turns_red_via_framework(tmp_path):
    pkg = tmp_path / "lore_vault"
    hooks = pkg / "hooks"
    hooks.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (hooks / "__init__.py").write_text("import numpy\n", encoding="utf-8")
    ctx = DoctorContext(settings={"hooks_dir": hooks})
    report = default_registry().run(ctx, categories=["hooks"])
    result = report.outcomes[0].result
    assert result.status is Status.FAIL
    assert result.counts["violations"] == 1
    assert any("numpy" in d for d in result.details)
    assert report.exit_code == EXIT_FAIL


def test_module_entry_point(tmp_path):
    """`python -m lore_vault.doctor --json` 實際可執行，輸出可解析的報告。"""
    result = subprocess.run(
        [sys.executable, "-m", "lore_vault.doctor", "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
    )
    assert result.returncode == EXIT_OK, result.stderr
    data = json.loads(result.stdout)
    assert "hooks.stdlib_only" in [c["name"] for c in data["checks"]]
