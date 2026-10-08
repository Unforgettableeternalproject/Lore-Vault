"""D15：核心（`lore_vault/` 除 `tasks/` 外）零 import 任務層。

純 AST 掃描（`lore_vault.tasks.isolation` 只用標準庫，不執行任何被掃描的模組）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lore_vault
from lore_vault.tasks.isolation import check_core_isolation

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
