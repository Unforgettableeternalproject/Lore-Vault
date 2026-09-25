"""doctor 對帳項：hook 路徑只用標準庫。

hook 由系統 Python 直接執行、不經 `.venv`。只要 hook 模組（或它的上層套件
`lore_vault/__init__.py`）import 了第三方套件或允許清單外的 `lore_vault` 子套件，
在系統 Python 下就會 ImportError——而且是在使用者編輯檔案的當下才爆。

掃描範圍：
- `lore_vault/hooks/` 全部 .py 與 `lore_vault/__init__.py`
- `agent_memory_spike/hook_*.py`，以及它們（遞迴）平鋪 import 的同目錄模組
- 允許清單內的 `lore_vault` 子套件（`binding`、`schema`）：被 import 到時
  遞迴掃整個子套件，確認它們本身也只用標準庫

這裡用 `ast` 靜態掃描，不實際 import hook 模組（hook 可能有副作用）。
在 doctor 框架中以 `hooks.stdlib_only` 註冊（見 `builtin.py`）。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOOKS_DIR = _PACKAGE_ROOT / "hooks"
DEFAULT_SPIKE_DIR = _PACKAGE_ROOT.parents[1] / "agent_memory_spike"
SPIKE_HOOK_GLOB = "hook_*.py"

# 允許的 lore_vault 內部 import。其他子套件（storage、api、config…）即使目前是
# 純標準庫，之後也可能長出第三方依賴，一律視為違規。
# binding／schema 已以 `python -S` 驗證只用標準庫，且會被遞迴掃描守住。
_ALLOWED_INTERNAL = ("lore_vault.hooks", "lore_vault.binding", "lore_vault.schema")
_ROOT_PACKAGE = "lore_vault"

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


def _allowed_package(module: str) -> str | None:
    """回傳 module 所屬的允許子套件名；`lore_vault` 本身回 `lore_vault`。"""
    if module == _ROOT_PACKAGE:
        return _ROOT_PACKAGE
    for allowed in _ALLOWED_INTERNAL:
        if module == allowed or module.startswith(allowed + "."):
            return allowed
    return None


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """列出所有絕對 import 的 (行號, 模組名)；相對 import 留在套件內，略過。"""
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


class _Scanner:
    def __init__(self, package_root: Path) -> None:
        # package_root：`lore_vault` 套件目錄（其下有 hooks/binding/schema）
        self.package_root = package_root
        self.report = HookImportReport(ok=True)
        self._seen: set[Path] = set()
        self._packages_done: set[str] = set()

    def scan_package(self, dotted: str) -> None:
        """掃一個允許的子套件（整個目錄）。`lore_vault` 本身只掃 `__init__.py`。"""
        if dotted in self._packages_done:
            return
        self._packages_done.add(dotted)
        if dotted == _ROOT_PACKAGE:
            init = self.package_root / "__init__.py"
            if init.is_file():
                self.scan_file(init, flat_dir=None)
            return
        # 載入子套件時一定會先執行上層 __init__.py
        self.scan_package(_ROOT_PACKAGE)
        directory = self.package_root / dotted.split(".", 1)[1].replace(".", "/")
        if not directory.is_dir():
            self.report.violations.append(Violation(directory, 0, f"<{dotted} 不存在>"))
            return
        for path in sorted(directory.rglob("*.py")):
            self.scan_file(path, flat_dir=None)

    def scan_file(self, path: Path, *, flat_dir: Path | None) -> None:
        """`flat_dir` 不為 None 時，bare import 可解析成該目錄下的同名 .py
        （spike 平鋪風格）。"""
        key = path.resolve()
        if key in self._seen:
            return
        self._seen.add(key)
        self.report.scanned.append(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            self.report.violations.append(Violation(path, 0, f"<無法解析：{exc}>"))
            return
        for lineno, module in _imported_modules(tree):
            root = module.split(".", 1)[0]
            if root in sys.stdlib_module_names:
                continue
            package = _allowed_package(module)
            if package is not None:
                self.scan_package(package)
                continue
            if flat_dir is not None and "." not in module:
                sibling = flat_dir / f"{module}.py"
                if sibling.is_file():
                    self.scan_file(sibling, flat_dir=flat_dir)
                    continue
            self.report.violations.append(Violation(path, lineno, module))


def check_hook_imports(
    hooks_dir: Path = DEFAULT_HOOKS_DIR,
    spike_dir: Path | None = None,
) -> HookImportReport:
    """掃描 `hooks_dir` 下所有 .py（與 `spike_dir` 的 `hook_*.py` 及其平鋪依賴），
    回報非標準庫、非允許清單的 import。

    `hooks_dir`／`spike_dir` 不存在或無法解析的檔案視為失敗，不當成「沒有違規」。
    `spike_dir` 為 None 時只掃 `hooks_dir`。
    """
    scanner = _Scanner(hooks_dir.parent)
    report = scanner.report
    if not hooks_dir.is_dir():
        report.violations.append(Violation(hooks_dir, 0, "<hooks 目錄不存在>"))
    else:
        # hook 進入點載入時一定會先執行上層套件的 __init__.py，一併檢查。
        scanner.scan_package(_ROOT_PACKAGE)
        scanner._packages_done.add("lore_vault.hooks")
        for path in sorted(hooks_dir.rglob("*.py")):
            scanner.scan_file(path, flat_dir=None)

    if spike_dir is not None:
        entries = sorted(spike_dir.glob(SPIKE_HOOK_GLOB)) if spike_dir.is_dir() else []
        if not entries:
            report.violations.append(
                Violation(spike_dir, 0, f"<找不到 {SPIKE_HOOK_GLOB}>")
            )
        for path in entries:
            scanner.scan_file(path, flat_dir=spike_dir)

    report.ok = not report.violations
    return report
