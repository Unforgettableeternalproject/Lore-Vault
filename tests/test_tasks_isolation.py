"""D15：核心（`lore_vault/` 除 `tasks/` 與具名例外 `mcp/task_plugin.py` 外）
零 import 任務層。

純 AST 掃描（`lore_vault.tasks.isolation` 只用標準庫，不執行任何被掃描的模組）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lore_vault
from lore_vault.tasks import isolation
from lore_vault.tasks.isolation import check_core_isolation, scan_file

PACKAGE_ROOT = Path(lore_vault.__file__).resolve().parent


def test_core_does_not_import_tasks():
    report = check_core_isolation(PACKAGE_ROOT)
    assert report.scanned, "沒有掃到任何檔案"
    assert not any(
        "tasks" in p.relative_to(PACKAGE_ROOT).parts[:1] for p in report.scanned
    )
    assert report.ok, [f"{v.path}:{v.lineno} {v.statement}" for v in report.violations]


def _fake_package(tmp_path: Path, rel: str, source: str) -> Path:
    pkg = tmp_path / "lore_vault"
    for d in (pkg, pkg / "notes", pkg / "tasks"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("", encoding="utf-8")
    (pkg / rel).write_text(source, encoding="utf-8")
    return pkg


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("notes/x.py", "import lore_vault.tasks\n"),
        ("notes/x.py", "import lore_vault.tasks.cli as c\n"),
        ("notes/x.py", "from lore_vault.tasks import cli\n"),
        ("notes/x.py", "from lore_vault import tasks\n"),
        ("notes/x.py", "from ..tasks import cli\n"),
        ("notes/x.py", "from .. import tasks\n"),
        ("__init__.py", "from . import tasks\n"),
        ("__init__.py", "from .tasks import cli\n"),
        ("notes/x.py", "def f():\n    import lore_vault.tasks.archive\n"),
        ("notes/x.py", "__import__('lore_vault.tasks')\n"),
        (
            "notes/x.py",
            "import importlib\nimportlib.import_module('lore_vault.tasks')\n",
        ),
    ],
)
def test_detects_every_import_form(tmp_path, rel, source):
    report = check_core_isolation(_fake_package(tmp_path, rel, source))
    assert not report.ok


@pytest.mark.parametrize(
    "source",
    [
        "from . import service\n",
        "import lore_vault.notes\n",
        "from lore_vault import notes\n",
        "x = 'lore_vault.tasks'\n",
    ],
)
def test_allows_unrelated_imports(tmp_path, source):
    assert check_core_isolation(_fake_package(tmp_path, "notes/x.py", source)).ok


def test_tasks_package_itself_is_excluded(tmp_path):
    pkg = _fake_package(tmp_path, "tasks/y.py", "from lore_vault.tasks import cli\n")
    assert check_core_isolation(pkg).ok


# ── 具名例外 `mcp/task_plugin.py`（TASK_LAYER_MCP §2 的三種紅燈情境）──────

_PLUGIN_LOAD = (
    "import importlib\n"
    "def _load():\n"
    "    return importlib.import_module('lore_vault.tasks.mcp_tools')\n"
)


def _with_mcp(tmp_path: Path, rel: str, source: str) -> Path:
    pkg = _fake_package(tmp_path, "notes/x.py", "")
    (pkg / "mcp").mkdir(exist_ok=True)
    (pkg / "mcp" / "__init__.py").write_text("", encoding="utf-8")
    target = pkg / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return pkg


def test_real_plugin_is_exempt_and_actually_imports_tasks():
    plugin = PACKAGE_ROOT / "mcp" / "task_plugin.py"
    report = check_core_isolation(PACKAGE_ROOT)
    assert plugin.is_file()
    assert plugin not in report.scanned
    # 例外必須是承重的：檔案本身確實 import 任務層，否則豁免就是多餘的洞
    assert scan_file(plugin, PACKAGE_ROOT)


def test_plugin_file_is_allowed(tmp_path):
    assert check_core_isolation(
        _with_mcp(tmp_path, "mcp/task_plugin.py", _PLUGIN_LOAD)
    ).ok


# 情境 1：非例外的核心檔案（含 mcp/server.py）import 任務層仍然紅
@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("mcp/server.py", "import lore_vault.tasks\n"),
        ("mcp/server.py", "from ..tasks import mcp_tools\n"),
        ("mcp/http.py", "from lore_vault.tasks.mcp_tools import build_tools\n"),
    ],
)
def test_other_mcp_files_still_violate(tmp_path, rel, source):
    report = check_core_isolation(_with_mcp(tmp_path, rel, source))
    assert not report.ok
    assert {v.path.name for v in report.violations} == {Path(rel).name}


# 情境 2：拿掉排除名單、檔案照舊 → 真實套件變紅，且違規正是 plugin
def test_removing_exemption_turns_real_package_red(monkeypatch):
    monkeypatch.setattr(isolation, "CORE_EXEMPT_FILES", ())
    report = check_core_isolation(PACKAGE_ROOT)
    assert not report.ok
    assert {v.path.relative_to(PACKAGE_ROOT).parts for v in report.violations} == {
        ("mcp", "task_plugin.py")
    }


# 情境 3：別的檔案做同樣的動態載入 → 仍然紅（豁免是單一路徑，不看檔名或目錄像不像）
@pytest.mark.parametrize(
    "rel",
    [
        "mcp/task_plugin2.py",
        "mcp/tasks_plugin.py",
        "mcp/sub/task_plugin.py",
        "task_plugin.py",
        "notes/task_plugin.py",
    ],
)
def test_same_loader_elsewhere_is_red(tmp_path, rel):
    report = check_core_isolation(_with_mcp(tmp_path, rel, _PLUGIN_LOAD))
    assert not report.ok
    assert [v.path for v in report.violations] == [tmp_path / "lore_vault" / rel]
