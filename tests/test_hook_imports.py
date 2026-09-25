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
