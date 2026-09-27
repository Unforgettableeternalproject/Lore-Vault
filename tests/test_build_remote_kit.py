"""scripts/build_remote_kit.py：kit 組裝（假 runner，不真的跑 uv build）。"""

from __future__ import annotations

import datetime as dt
import importlib.util
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
            (out / "lore_vault-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
            (out / ".gitignore").write_text("*")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def test_build_kit_assembles_contents(tmp_path):
    runner = FakeRunner()
    kit = kit_mod.build_kit(tmp_path, runner=runner, today=dt.date(2026, 9, 27))
    assert kit.name == "lore-vault-kit-0.1.0-20260927-abc1234"
    names = sorted(p.name for p in kit.iterdir())
    assert names == [
        "README.txt",
        "SKILL.md",
        "install.py",
        "lore_vault-0.1.0-py3-none-any.whl",
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
    with zipfile.ZipFile(tmp_path / f"{kit.name}.zip") as zf:
        assert sorted(zf.namelist()) == sorted(f"{kit.name}/{n}" for n in names)


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
