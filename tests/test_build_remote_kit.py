"""scripts/build_remote_kit.py：kit 組裝（假 runner，不真的跑 uv build）。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    path = REPO / "scripts" / "build_remote_kit.py"
    spec = importlib.util.spec_from_file_location("lv_build_remote_kit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


kit_mod = _load()


class FakeRunner:
    def __init__(self, *, dirty=False, build_ok=True):
        self.calls = []
        self.dirty = dirty
        self.build_ok = build_ok

    def __call__(self, argv, cwd):
        argv = list(argv)
        self.calls.append((argv, cwd))
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "abc1234\n", "")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, " M src/x.py\n" if self.dirty else "", ""
            )
        if argv[:2] == ["uv", "build"]:
            if not self.build_ok:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            out = Path(argv[argv.index("-o") + 1])
            (out / "lore_vault-0.1.1-py3-none-any.whl").write_bytes(b"wheel")
            (out / ".gitignore").write_text("*")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def test_build_kit_assembles_contents(tmp_path):
    runner = FakeRunner()
    kit = kit_mod.build_kit(tmp_path, runner=runner, today=dt.date(2026, 9, 27))
    assert kit.name == "lore-vault-kit-0.1.1-20260927-abc1234"
    names = sorted(p.name for p in kit.iterdir())
    assert names == [
        "README.txt",
        "SKILL.md",
        "hooks",
        "install.py",
        "lore_vault-0.1.1-py3-none-any.whl",
    ]
    assert (kit / "SKILL.md").read_bytes() == (
        REPO / "integrations/claude/skills/pm/SKILL.md"
    ).read_bytes()
    assert (kit / "install.py").read_bytes() == (
        REPO / "integrations/remote/install.py"
    ).read_bytes()
    readme = (kit / "README.txt").read_text(encoding="utf-8")
    import hashlib

    assert hashlib.sha256(b"wheel").hexdigest() in readme
    assert "python install.py" in readme
    # wheel 輸出到 kit，不寫 repo 的 dist/
    build = next(a for a, _ in runner.calls if a[:2] == ["uv", "build"])
    assert build[-2:] == ["-o", str(kit)]
    files = sorted(p.relative_to(kit).as_posix() for p in kit.rglob("*") if p.is_file())
    with zipfile.ZipFile(tmp_path / f"{kit.name}.zip") as zf:
        assert sorted(zf.namelist()) == [f"{kit.name}/{n}" for n in files]
    assert f"{kit.name}/hooks/spike/hook_stop.py" in zf.namelist()


def test_build_kit_marks_dirty(tmp_path):
    kit = kit_mod.build_kit(
        tmp_path,
        runner=FakeRunner(dirty=True),
        today=dt.date(2026, 1, 2),
        make_zip=False,
    )
    assert kit.name.endswith("-abc1234-dirty")
    assert not (tmp_path / f"{kit.name}.zip").exists()


def test_build_kit_refuses_existing_without_force(tmp_path):
    kit_mod.build_kit(tmp_path, runner=FakeRunner(), make_zip=False)
    with pytest.raises(SystemExit):
        kit_mod.build_kit(tmp_path, runner=FakeRunner(), make_zip=False)
    kit_mod.build_kit(tmp_path, runner=FakeRunner(), make_zip=False, force=True)


def test_build_kit_stops_on_build_failure(tmp_path):
    with pytest.raises(SystemExit):
        kit_mod.build_kit(tmp_path, runner=FakeRunner(build_ok=False), make_zip=False)


# ── episode hook 子集（D13）──


def _hook_scan():
    from lore_vault.doctor.hook_imports import check_hook_imports

    return check_hook_imports(
        hooks_dir=REPO / "src" / "lore_vault" / "hooks",
        spike_dir=REPO / "agent_memory_spike",
    )


@pytest.fixture(scope="module")
def built_kit(tmp_path_factory):
    out = tmp_path_factory.mktemp("kit")
    return kit_mod.build_kit(out, runner=FakeRunner(), make_zip=False)


def test_kit_hooks_follow_doctor_boundary(built_kit):
    """kit 的 hook 檔案＝doctor hooks.stdlib_only 掃到的集合，不多不少。"""
    hooks = built_kit / "hooks"
    manifest = json.loads((hooks / "VERSION.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.1.1"
    assert manifest["commit"] == "abc1234"
    scan = _hook_scan()
    assert scan.ok
    expected = set()
    for path in scan.scanned:
        rel = path.resolve().relative_to(REPO.resolve()).as_posix()
        if rel.startswith("agent_memory_spike/"):
            expected.add("spike/" + rel.removeprefix("agent_memory_spike/"))
        else:
            expected.add(rel)  # src/lore_vault/...
    assert set(manifest["files"]) == expected
    on_disk = {
        p.relative_to(hooks).as_posix()
        for p in hooks.rglob("*")
        if p.is_file() and p.name != "VERSION.json"
    }
    assert on_disk == expected
    for rel in (
        "spike/hook_stop.py",
        "spike/hook_pretooluse.py",
        "spike/paths.py",
        "src/lore_vault/__init__.py",
        "src/lore_vault/hooks/client_env.py",
    ):
        assert rel in manifest["files"]
    # 服務端套件（storage、api、config…）不在 kit
    assert not any(
        r.startswith("src/lore_vault/")
        and r.split("/")[2] not in ("__init__.py", "hooks", "binding", "schema")
        for r in manifest["files"]
    )
    # 安裝器能驗證這份 manifest
    inst = kit_mod.load_installer(REPO)
    assert inst.load_hooks_manifest(hooks)["files"] == manifest["files"]


def test_kit_stops_when_hook_boundary_violated(tmp_path, monkeypatch):
    from lore_vault.doctor import hook_imports

    def bad_scan(**_kw):
        report = hook_imports.HookImportReport(ok=False)
        report.violations.append(hook_imports.Violation(Path("x.py"), 3, "httpx"))
        return report

    monkeypatch.setattr(hook_imports, "check_hook_imports", bad_scan)
    with pytest.raises(SystemExit, match="httpx"):
        kit_mod.build_kit(tmp_path, runner=FakeRunner(), make_zip=False)


def _isolated_run(script: Path, args: list[str], home: Path, client_env: Path):
    """以 `-I -S` 執行：沒有 site-packages，也不讀 PYTHON* 環境變數——
    證明 kit 佈局下 hook 只靠 hooks/src 就 import 得到 lore_vault.hooks。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LORE_VAULT", "CF_ACCESS", "PYTHON"))
    }
    env.update(LORE_VAULT_SPIKE_HOME=str(home), LORE_VAULT_CLIENT_ENV=str(client_env))
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_kit_hooks_run_without_site_packages(built_kit, tmp_path):
    home = tmp_path / "lv"
    home.mkdir()
    client_env = tmp_path / "client.env"
    client_env.write_bytes(
        b"LORE_VAULT_URL=http://127.0.0.1:9\nLORE_VAULT_API_TOKEN=x\n"
    )
    spike = built_kit / "hooks" / "spike"
    stop = _isolated_run(
        spike / "hook_stop.py", ["--push", "--dry-run"], home, client_env
    )
    assert stop.returncode == 0, stop.stderr
    assert "[spool]" in stop.stderr
    assert "推送已設定" in stop.stderr
    pre = _isolated_run(spike / "hook_pretooluse.py", ["--stats"], home, client_env)
    assert pre.returncode == 0, pre.stderr
    assert "ModuleNotFoundError" not in pre.stderr + stop.stderr
