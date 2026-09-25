"""T-07／T-08：hook 路徑只用標準庫。

- 靜態：doctor 檢查在臨時 hook 目錄加入第三方 import 時必須變紅（不改真的 hook 模組）
- 動態：在不含 site-packages 的直譯器（`-S`）下 import `lore_vault.hooks` 必須成功，
  且不載入任何非標準庫模組
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lore_vault.doctor.hook_imports import DEFAULT_HOOKS_DIR, check_hook_imports

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _make_hooks(tmp_path: Path, source: str, name: str = "hook_x.py") -> Path:
    """建立一個仿 `lore_vault/hooks` 的臨時套件，回傳 hooks 目錄。"""
    pkg = tmp_path / "lore_vault"
    hooks = pkg / "hooks"
    hooks.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""root"""\n', encoding="utf-8")
    (hooks / "__init__.py").write_text("", encoding="utf-8")
    (hooks / name).write_text(textwrap.dedent(source), encoding="utf-8")
    return hooks


def test_real_hooks_package_passes():
    report = check_hook_imports(DEFAULT_HOOKS_DIR)
    assert report.ok, report.violations
    # 確認真的掃到東西，而不是空目錄靜默通過
    assert any(p.name == "__init__.py" for p in report.scanned)
    assert DEFAULT_HOOKS_DIR.parent / "__init__.py" in report.scanned


def test_third_party_import_turns_red(tmp_path):
    hooks = _make_hooks(tmp_path, "import json\nimport numpy\n")
    report = check_hook_imports(hooks)
    assert not report.ok
    assert [(v.lineno, v.module) for v in report.violations] == [(2, "numpy")]


@pytest.mark.parametrize(
    "source, module",
    [
        ("from numpy import array\n", "numpy"),
        ("def f():\n    import fastapi\n", "fastapi"),
        ("import lore_vault.storage\n", "lore_vault.storage"),
        ("from lore_vault.api import app\n", "lore_vault.api"),
        ("import importlib\nimportlib.import_module('numpy')\n", "numpy"),
    ],
)
def test_forbidden_import_variants_turn_red(tmp_path, source, module):
    report = check_hook_imports(_make_hooks(tmp_path, source))
    assert not report.ok
    assert module in [v.module for v in report.violations]


def test_allowed_imports_pass(tmp_path):
    source = """
        import json, os.path
        from pathlib import Path
        from . import sibling
        from .sibling import thing
        import lore_vault.hooks
        from lore_vault.hooks.common import helper
    """
    report = check_hook_imports(_make_hooks(tmp_path, source))
    assert report.ok, report.violations


def test_parent_package_init_is_checked(tmp_path):
    hooks = _make_hooks(tmp_path, "import json\n")
    (hooks.parent / "__init__.py").write_text("import fastapi\n", encoding="utf-8")
    report = check_hook_imports(hooks)
    assert not report.ok
    assert report.violations[0].path == hooks.parent / "__init__.py"


def test_missing_dir_or_unparsable_file_is_red(tmp_path):
    assert not check_hook_imports(tmp_path / "nope").ok
    assert not check_hook_imports(_make_hooks(tmp_path, "def broken(:\n")).ok


def test_hooks_import_without_site_packages():
    """`-S` 停用 site-packages：等同系統 Python 沒裝任何第三方套件的情境。"""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SRC_DIR)!r})
        import lore_vault.hooks
        extra = sorted(
            m for m in sys.modules
            if m.split(".")[0] not in sys.stdlib_module_names
            and m.split(".")[0] not in ("lore_vault", "__main__")
        )
        print(",".join(extra))
        """
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# ── 階段 8：spike hook（平鋪）與允許的 binding／schema ─────────────────


REPO_ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = REPO_ROOT / "agent_memory_spike"


def _make_layout(tmp_path: Path, spike_files: dict[str, str], pkg_files=None):
    """仿 repo 佈局：`src/lore_vault/{hooks,binding,...}` + 平鋪的 spike 目錄。"""
    hooks = _make_hooks(tmp_path / "src", "import json\n")
    for rel, source in (pkg_files or {}).items():
        path = hooks.parent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    spike = tmp_path / "spike"
    spike.mkdir()
    for name, source in spike_files.items():
        (spike / name).write_text(textwrap.dedent(source), encoding="utf-8")
    return hooks, spike


def test_real_tree_with_spike_hooks_passes():
    report = check_hook_imports(DEFAULT_HOOKS_DIR, SPIKE_DIR)
    assert report.ok, report.violations
    names = {p.name for p in report.scanned}
    # 進入點、它們平鋪 import 的模組、允許的 binding／schema 都真的掃到
    assert {"hook_stop.py", "hook_pretooluse.py", "transcript.py", "paths.py"} <= names
    assert {"remote.py", "models.py", "spool.py", "client_env.py"} <= names


def test_spike_transitive_third_party_import_turns_red(tmp_path):
    """進入點本身乾淨、它平鋪 import 的同目錄模組用了第三方 → 紅。"""
    hooks, spike = _make_layout(
        tmp_path,
        {
            "hook_x.py": "import sibling\n",
            "sibling.py": "import json\nimport numpy\n",
            "unrelated.py": "import fastapi\n",  # 沒被 hook import 的不掃
        },
    )
    report = check_hook_imports(hooks, spike)
    assert not report.ok
    assert [(v.path.name, v.module) for v in report.violations] == [
        ("sibling.py", "numpy")
    ]


def test_spike_unknown_bare_import_is_red(tmp_path):
    hooks, spike = _make_layout(tmp_path, {"hook_x.py": "import requests\n"})
    report = check_hook_imports(hooks, spike)
    assert [v.module for v in report.violations] == ["requests"]


def test_allowed_internal_packages_are_scanned_recursively(tmp_path):
    clean = {
        "binding/__init__.py": "from .remote import x\n",
        "binding/remote.py": "import subprocess\nfrom lore_vault.schema import V\n",
        "schema/__init__.py": "import dataclasses\n",
    }
    hooks, spike = _make_layout(
        tmp_path,
        {"hook_x.py": "from lore_vault.binding import resolve_binding\n"},
        clean,
    )
    assert check_hook_imports(hooks, spike).ok

    dirty = dict(clean, **{"schema/__init__.py": "import pydantic\n"})
    hooks, spike = _make_layout(
        tmp_path / "b",
        {"hook_x.py": "from lore_vault.binding import resolve_binding\n"},
        dirty,
    )
    report = check_hook_imports(hooks, spike)
    assert [v.module for v in report.violations] == ["pydantic"]


def test_other_internal_package_from_spike_is_red(tmp_path):
    hooks, spike = _make_layout(
        tmp_path, {"hook_x.py": "from lore_vault.config import load_config\n"}
    )
    assert [v.module for v in check_hook_imports(hooks, spike).violations] == [
        "lore_vault.config"
    ]


def test_explicit_spike_dir_without_hooks_is_red(tmp_path):
    hooks = _make_hooks(tmp_path / "src", "import json\n")
    assert not check_hook_imports(hooks, tmp_path / "missing").ok
    (tmp_path / "empty").mkdir()
    assert not check_hook_imports(hooks, tmp_path / "empty").ok


@pytest.mark.parametrize(
    "args",
    [
        ["hook_stop.py", "--push", "--dry-run"],
        ["hook_pretooluse.py", "--stats"],
    ],
)
def test_spike_hook_entrypoints_run_without_site_packages(tmp_path, args):
    """系統 Python 直接執行的情境：`-S` 停用 site-packages、不經安裝，
    進入點靠自身位置找到 `src/`，整條 import 鏈（含 lore_vault.hooks／binding）
    起得來。"""
    import os

    home = tmp_path / "home"
    home.mkdir()
    # 機器上若設了真的 token／client.env，行為會漂移：LORE_VAULT*／CF_ACCESS* 一律剝掉
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTHONPATH" and not k.startswith(("LORE_VAULT", "CF_ACCESS"))
    }
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-S", str(SPIKE_DIR / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    # 沒有寫出任何東西到家目錄以外；--dry-run／--stats 也不該建資料目錄
    assert not (home / ".claude" / "agent-memory-spike" / "spool").exists()


def test_doctor_check_turns_red_for_spike_hook(tmp_path):
    from lore_vault.doctor import DoctorContext, Status, default_registry

    hooks, spike = _make_layout(tmp_path, {"hook_stop.py": "import numpy\n"})
    report = default_registry().run(
        DoctorContext(settings={"hooks_dir": str(hooks), "spike_dir": str(spike)}),
        categories=["hooks"],
    )
    [outcome] = report.outcomes
    assert outcome.result.status is Status.FAIL
    assert any("numpy" in d for d in outcome.result.details)
