"""核心零 import 任務層的機械保證（純標準庫 AST 掃描，不實際 import 任何模組）。

掃描 `lore_vault/` 底下除了 `tasks/` 以外的全部 .py，凡指向 `lore_vault.tasks` 的
import 都算違規，包括：
- `import lore_vault.tasks`、`from lore_vault.tasks import x`、
  `from lore_vault import tasks`
- 相對 import：`from . import tasks`（在 `lore_vault/__init__.py`）、
  `from ..tasks import x`
- 以字串動態 import：`__import__("lore_vault.tasks")`、
  `import_module("lore_vault.tasks")`

`tests/test_tasks_isolation.py` 與 doctor `tasks.isolation` 共用這裡。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "lore_vault"
TASKS = "tasks"
DEFAULT_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_DYNAMIC = {"__import__", "import_module"}


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    statement: str


@dataclass
class IsolationReport:
    scanned: list[Path] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _is_tasks(parts: list[str], names: list[str] | None = None) -> bool:
    if parts[:2] == [PACKAGE, TASKS]:
        return True
    return parts == [PACKAGE] and bool(names) and TASKS in (names or [])


def _module_package(path: Path, package_root: Path) -> list[str]:
    rel = path.relative_to(package_root).with_suffix("")
    parts = [PACKAGE, *rel.parts]
    # 模組檔所屬套件是其目錄；`__init__.py` 的套件就是它自己的目錄
    return parts[:-1]


def scan_file(path: Path, package_root: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_package(path, package_root)
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_tasks(alias.name.split(".")):
                    found.append(Violation(path, node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
            if node.level == 0:
                base = (node.module or "").split(".")
            else:
                keep = len(package) - (node.level - 1)
                if keep <= 0:
                    continue
                base = package[:keep] + (node.module.split(".") if node.module else [])
            if _is_tasks(base, names):
                dots = "." * node.level
                found.append(
                    Violation(
                        path,
                        node.lineno,
                        f"from {dots}{node.module or ''} import {', '.join(names)}",
                    )
                )
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if (
                name in _DYNAMIC
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_tasks(node.args[0].value.split("."))
            ):
                found.append(
                    Violation(path, node.lineno, f"{name}({node.args[0].value!r})")
                )
    return found


def check_core_isolation(package_root: Path = DEFAULT_PACKAGE_ROOT) -> IsolationReport:
    """`package_root` 是 `lore_vault` 套件目錄；排除其下的 `tasks/`。"""
    report = IsolationReport()
    tasks_dir = package_root / TASKS
    if not package_root.is_dir():
        report.violations.append(Violation(package_root, 0, "<套件目錄不存在>"))
        return report
    for path in sorted(package_root.rglob("*.py")):
        if tasks_dir in path.parents:
            continue
        report.scanned.append(path)
        try:
            report.violations.extend(scan_file(path, package_root))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            report.violations.append(Violation(path, 0, f"<無法解析：{exc}>"))
    return report
