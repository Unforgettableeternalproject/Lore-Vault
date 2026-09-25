"""doctor 對帳項：hook 路徑只用標準庫。

hook 由系統 Python 直接執行、不經 `.venv`。只要 hook 模組（或它的上層套件
`lore_vault/__init__.py`）import 了第三方套件或其他 `lore_vault` 子套件，
在系統 Python 下就會 ImportError——而且是在使用者編輯檔案的當下才爆。

這裡用 `ast` 靜態掃描，不實際 import hook 模組（hook 可能有副作用）。
之後 T-14 的 doctor 框架建立後，把 `check_hook_imports` 註冊成一個檢查項。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOOKS_DIR = _PACKAGE_ROOT / "hooks"

# 允許的 lore_vault 內部 import：只有 hooks 自己。其他子套件（storage、api…）
# 即使目前是純標準庫，之後也可能長出第三方依賴，一律視為違規。
_ALLOWED_INTERNAL = "lore_vault.hooks"

# 以字串動態 import 的呼叫，也要檢查字面值參數。
_DYNAMIC_IMPORT_CALLS = {"__import__", "import_module"}


@dataclass(frozen=True)
class Violation:
    """一筆違規 import。"""

    path: Path
    lineno: int
    module: str


@dataclass
class HookImportReport:
    """檢查結果：`ok` 為 False 時 `violations` 列出每一筆違規。"""

    ok: bool
    scanned: list[Path] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


def _is_allowed(module: str) -> bool:
    root = module.split(".", 1)[0]
    if root in sys.stdlib_module_names:
        return True
    return module == _ALLOWED_INTERNAL or module.startswith(_ALLOWED_INTERNAL + ".")


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """列出所有絕對 import 的 (行號, 模組名)；相對 import 留在 hooks 內，略過。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append((node.lineno, node.module))
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
                name in _DYNAMIC_IMPORT_CALLS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and not node.args[0].value.startswith(".")
            ):
                found.append((node.lineno, node.args[0].value))
    return found


def _files_to_scan(hooks_dir: Path) -> list[Path]:
    files = sorted(hooks_dir.rglob("*.py"))
    # hook 進入點載入時一定會先執行上層套件的 __init__.py，一併檢查。
    parent_init = hooks_dir.parent / "__init__.py"
    if parent_init.is_file():
        files.insert(0, parent_init)
    return files


def check_hook_imports(hooks_dir: Path = DEFAULT_HOOKS_DIR) -> HookImportReport:
    """掃描 `hooks_dir` 下所有 .py，回報非標準庫、非 hooks 內部的 import。

    `hooks_dir` 不存在或無法解析的檔案視為失敗，不當成「沒有違規」。
    """
    report = HookImportReport(ok=True)
    if not hooks_dir.is_dir():
        report.ok = False
        report.violations.append(Violation(hooks_dir, 0, "<hooks 目錄不存在>"))
        return report

    for path in _files_to_scan(hooks_dir):
        report.scanned.append(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            report.violations.append(Violation(path, 0, f"<無法解析：{exc}>"))
            continue
        for lineno, module in _imported_modules(tree):
            if not _is_allowed(module):
                report.violations.append(Violation(path, lineno, module))

    report.ok = not report.violations
    return report
