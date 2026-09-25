"""內建檢查項清單：`default_registry()` 每次回傳新的 Registry。

新增檢查項：在對應模組寫 `(ctx) -> CheckResult` 函式，再到 `default_registry()`
加一行 `registry.add(Check(...))`。見 docs/DEVELOPMENT.md「新增 doctor 檢查項」。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lore_vault.storage import checks as storage_checks
from lore_vault.storage import enrichment as storage_enrichment
from lore_vault.storage import imports as storage_imports
from lore_vault.storage import ingest_checks as storage_ingest

from .backup_check import backup_recent
from .concept_snapshot_check import concept_snapshot_age
from .framework import Check, CheckResult, CheckSkipped, DoctorContext, Registry
from .hook_imports import DEFAULT_HOOKS_DIR, DEFAULT_SPIKE_DIR, check_hook_imports
from .snapshot_check import snapshot_age, snapshot_schema
from .spool_check import spool_conflicts, spool_pending


def hooks_stdlib_only(ctx: DoctorContext) -> CheckResult:
    """hook 路徑只 import 標準庫與允許的 `lore_vault` 子套件。

    設定鍵 `hooks_dir` 可覆寫掃描目錄；`spike_dir` 指定 spike hook 目錄
    （明確指定卻不存在為 fail）。未指定時用 repo 內的 `agent_memory_spike/`，
    不存在（如 docker 映像）則只掃 `hooks_dir`。
    """
    hooks_dir = Path(ctx.settings.get("hooks_dir", DEFAULT_HOOKS_DIR))
    spike_setting = ctx.settings.get("spike_dir")
    if spike_setting is not None:
        spike_dir: Path | None = Path(spike_setting)
    else:
        spike_dir = DEFAULT_SPIKE_DIR if DEFAULT_SPIKE_DIR.is_dir() else None
    report = check_hook_imports(hooks_dir, spike_dir)
    counts = {"scanned": len(report.scanned), "violations": len(report.violations)}
    if report.ok:
        scope = "" if spike_dir is not None else "（無 spike 目錄，只掃 hooks）"
        return CheckResult.ok(
            f"掃描 {len(report.scanned)} 個檔案{scope}", counts=counts
        )
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


def storage_missing_summaries(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.missing_summaries(ctx.require("db")))


def storage_vector_dimension(ctx: DoctorContext) -> CheckResult:
    db = ctx.require("db")
    return _to_result(storage_checks.vector_dimension(db, dim=_embedding_dim(ctx)))


# ── 背景補算對帳（資源 "db"；設定 "enrich_backlog_max_age"：秒，預設 3600；
#    "now"：datetime，測試注入用）──

DEFAULT_BACKLOG_MAX_AGE = 3600.0


def enrich_failed(ctx: DoctorContext) -> CheckResult:
    try:
        return _to_result(storage_enrichment.failed_enrichments(ctx.require("db")))
    except storage_enrichment.MissingEnrichmentTable as exc:
        raise CheckSkipped(str(exc)) from None


def enrich_backlog(ctx: DoctorContext) -> CheckResult:
    now = ctx.settings.get("now") or datetime.now(UTC)
    max_age = float(ctx.settings.get("enrich_backlog_max_age", DEFAULT_BACKLOG_MAX_AGE))
    try:
        rec = storage_enrichment.enrichment_backlog(
            ctx.require("db"), now=now, max_age_seconds=max_age
        )
    except storage_enrichment.MissingEnrichmentTable as exc:
        raise CheckSkipped(str(exc)) from None
    return _to_result(rec)


# ── 匯入對帳（資源 "db"；設定 "import_source"：來源名稱，預設 open-notebook）──

DEFAULT_IMPORT_SOURCE = "open-notebook"


def import_on_reconcile(ctx: DoctorContext) -> CheckResult:
    db = ctx.require("db")
    source = str(ctx.settings.get("import_source", DEFAULT_IMPORT_SOURCE))
    try:
        if source not in storage_imports.import_sources(db):
            raise CheckSkipped(f"沒有 {source} 的匯入對帳清單（尚未匯入）")
        return _to_result(storage_imports.reconcile(db, source))
    except storage_imports.MissingImportTables as exc:
        raise CheckSkipped(str(exc)) from None


# ── spike 接入對帳（資源 "db"；設定 "now"、"episode_ingest_max_age_hours"（預設 48）、
#    "auto_vault_warn_above"（未設＝只報數））──


def episodes_ingest_recency(ctx: DoctorContext) -> CheckResult:
    now = ctx.settings.get("now") or datetime.now(UTC)
    max_age = float(
        ctx.settings.get(
            "episode_ingest_max_age_hours",
            storage_ingest.DEFAULT_EPISODE_INGEST_MAX_AGE_HOURS,
        )
    )
    return _to_result(
        storage_ingest.episode_ingest_recency(
            ctx.require("db"), now=now, max_age_hours=max_age
        )
    )


def vaults_auto_created(ctx: DoctorContext) -> CheckResult:
    warn_above = ctx.settings.get("auto_vault_warn_above")
    return _to_result(
        storage_ingest.auto_created_vaults(
            ctx.require("db"),
            warn_above=None if warn_above is None else int(warn_above),
        )
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
            "storage.missing_summaries",
            storage_missing_summaries,
            "缺 summary 的 note 數（非零為 warn）",
        ),
        (
            "storage.vector_dimension",
            storage_vector_dimension,
            "向量維度與設定 embedding_dim 一致",
        ),
    ):
        registry.add(Check(name, "storage", func, description))
    for name, func, description in (
        (
            "enrich.failed",
            enrich_failed,
            "補算超過重試上限的項目數（非零為 fail：不會自癒，需人工處理後 reset）",
        ),
        (
            "enrich.backlog",
            enrich_backlog,
            "待補算積壓；最舊一筆等太久為 warn（worker 可能沒在跑）",
        ),
    ):
        registry.add(Check(name, "enrich", func, description))
    registry.add(
        Check(
            "import.on_reconcile",
            "import",
            import_on_reconcile,
            "Open Notebook 匯入對帳：漏筆、內容竄改、各 vault 筆數與來源一致",
        )
    )
    registry.add(
        Check(
            "backup.recent",
            "backup",
            backup_recent,
            "最近一次備份在門檻內（從未備份為 fail）",
        )
    )
    for name, func, description in (
        (
            "snapshot.schema_version",
            snapshot_schema,
            "本地快照與 manifest 一致、schema 版本等於程式預期",
        ),
        (
            "snapshot.age",
            snapshot_age,
            "本地快照產生時間在門檻內（從未拉取為 fail）",
        ),
    ):
        registry.add(Check(name, "snapshot", func, description))
    registry.add(
        Check(
            "episodes.ingest_recency",
            "episodes",
            episodes_ingest_recency,
            "各機器 episode 筆數與最近收料時間（超過門檻或從未收料為 warn）",
        )
    )
    registry.add(
        Check(
            "vaults.auto_created",
            "vaults",
            vaults_auto_created,
            "episode 收料／管線自動建立的 vault 數與來源（供審視）",
        )
    )
    for name, func, description in (
        (
            "spool.pending",
            spool_pending,
            "episode spool 未推送筆數與最舊一筆年齡"
            "（超過門檻 warn／fail；推送未設定為 warn）",
        ),
        (
            "spool.conflicts",
            spool_conflicts,
            "服務拒收（conflict／invalid）或損毀而留在 spool 的筆數（非零為 fail）",
        ),
    ):
        registry.add(Check(name, "spool", func, description))
    registry.add(
        Check(
            "concept_snapshot.age",
            "concept_snapshot",
            concept_snapshot_age,
            "PreToolUse 用的 concept 快照與 manifest 一致且在年齡門檻內",
        )
    )
    return registry
