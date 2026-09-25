"""內建檢查項清單：`default_registry()` 每次回傳新的 Registry。

新增檢查項：在對應模組寫 `(ctx) -> CheckResult` 函式，再到 `default_registry()`
加一行 `registry.add(Check(...))`。見 docs/DEVELOPMENT.md「新增 doctor 檢查項」。
"""

from __future__ import annotations

from pathlib import Path

from .framework import Check, CheckResult, DoctorContext, Registry
from .hook_imports import DEFAULT_HOOKS_DIR, check_hook_imports


def hooks_stdlib_only(ctx: DoctorContext) -> CheckResult:
    """hook 路徑只 import 標準庫與 `lore_vault.hooks`。

    設定鍵 `hooks_dir` 可覆寫掃描目錄。
    """
    hooks_dir = Path(ctx.settings.get("hooks_dir", DEFAULT_HOOKS_DIR))
    report = check_hook_imports(hooks_dir)
    counts = {"scanned": len(report.scanned), "violations": len(report.violations)}
    if report.ok:
        return CheckResult.ok(f"掃描 {len(report.scanned)} 個檔案", counts=counts)
    return CheckResult.fail(
        f"{len(report.violations)} 筆非標準庫 import",
        details=[f"{v.path}:{v.lineno} {v.module}" for v in report.violations],
        counts=counts,
    )


def default_registry() -> Registry:
    registry = Registry()
    registry.add(
        Check(
            "hooks.stdlib_only",
            "hooks",
            hooks_stdlib_only,
            "hook 路徑只用標準庫（系統 Python 直接執行）",
        )
    )
    return registry
