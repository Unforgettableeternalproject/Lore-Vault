"""內建檢查項清單：`default_registry()` 每次回傳新的 Registry。

新增檢查項：在對應模組寫 `(ctx) -> CheckResult` 函式，再到 `default_registry()`
加一行 `registry.add(Check(...))`。見 docs/DEVELOPMENT.md「新增 doctor 檢查項」。
"""

from __future__ import annotations

from pathlib import Path

from lore_vault.storage import checks as storage_checks

from .framework import Check, CheckResult, CheckSkipped, DoctorContext, Registry
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


# ── 儲存層對帳（資源 "db"：sqlite3 連線；設定 "embedding_dim"：向量維度）──


def _to_result(rec: storage_checks.Reconciliation) -> CheckResult:
    factory = {
        "pass": CheckResult.ok,
        "warn": CheckResult.warn,
        "fail": CheckResult.fail,
    }[rec.status]
    return factory(rec.summary, details=rec.details, counts=rec.counts)


def _embedding_dim(ctx: DoctorContext) -> int:
    dim = ctx.settings.get("embedding_dim")
    if dim is None:
        raise CheckSkipped("缺少設定：embedding_dim")
    return int(dim)


def storage_schema_version(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.schema_version(ctx.require("db")))


def storage_fts_rows(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.fts_rows(ctx.require("db")))


def storage_missing_embeddings(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.missing_embeddings(ctx.require("db")))


def storage_vector_dimension(ctx: DoctorContext) -> CheckResult:
    db = ctx.require("db")
    return _to_result(storage_checks.vector_dimension(db, dim=_embedding_dim(ctx)))


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
    for name, func, description in (
        (
            "storage.schema_version",
            storage_schema_version,
            "資料庫 schema 版本與程式預期一致",
        ),
        ("storage.fts_rows", storage_fts_rows, "FTS 索引列與 note 一對一"),
        (
            "storage.missing_embeddings",
            storage_missing_embeddings,
            "缺 embedding 的 note 數（非零為 warn）",
        ),
        (
            "storage.vector_dimension",
            storage_vector_dimension,
            "向量維度與設定 embedding_dim 一致",
        ),
    ):
        registry.add(Check(name, "storage", func, description))
    return registry
