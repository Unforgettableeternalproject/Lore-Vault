"""內建檢查項清單：`default_registry()` 每次回傳新的 Registry。

新增檢查項：在對應模組寫 `(ctx) -> CheckResult` 函式，再到 `default_registry()`
加一行 `registry.add(Check(...))`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lore_vault.storage import checks as storage_checks
from lore_vault.storage import enrichment as storage_enrichment
from lore_vault.storage import imports as storage_imports
from lore_vault.storage import ingest_checks as storage_ingest
from lore_vault.storage import manage as storage_manage
from lore_vault.storage import settings_store as storage_settings
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage import ui_login as storage_ui_login

from .backup_check import backup_recent
from .concept_push_check import concept_push_lag
from .concept_scope_check import concept_scope_anchor_agreement
from .concept_snapshot_check import (
    concept_snapshot_age,
    concept_snapshot_path_agreement,
)
from .documents_check import (
    documents_backlog,
    documents_blob_exists,
    documents_chunk_count,
    documents_failed,
    documents_fts_rows,
    documents_orphan_blobs,
    documents_quality_warnings,
    documents_stuck,
    documents_superseded_removed,
    documents_vector_rows,
)
from .episode_pull_check import episode_pull_status
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


def storage_control_chars(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.control_chars(ctx.require("db")))


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


def enrich_queue_time(ctx: DoctorContext) -> CheckResult:
    try:
        return _to_result(storage_enrichment.queue_time_integrity(ctx.require("db")))
    except storage_enrichment.MissingEnrichmentTable as exc:
        raise CheckSkipped(str(exc)) from None


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


def _episodes_ingest_enabled(ctx: DoctorContext) -> bool | None:
    """收料開關的有效值：設定鍵 `episodes_ingest`（服務依執行期設定填）優先；
    未提供時（doctor CLI）看 DB 覆寫；都沒有回 None（不知道，照常檢查）。"""
    value = ctx.settings.get("episodes_ingest")
    if value is not None:
        return bool(value)
    override = storage_settings.read_override(ctx.require("db"), "episodes.ingest")
    return override if isinstance(override, bool) else None


def episodes_ingest_recency(ctx: DoctorContext) -> CheckResult:
    if _episodes_ingest_enabled(ctx) is False:
        raise CheckSkipped("服務未開啟 episode 收料（episodes.ingest = false）")
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


def vaults_misc_routing(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_ingest.misc_routing(ctx.require("db")))


def space_valid_values(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.space_valid_values(ctx.require("db")))


def space_key_prefix_agreement(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_checks.space_key_prefix_agreement(ctx.require("db")))


def vaults_alias_integrity(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_manage.alias_integrity(ctx.require("db")))


def sidecar_orphans(ctx: DoctorContext) -> CheckResult:
    """側載列（schema v17）指向現存且 space 相符的 vault；v17 前的庫為 skipped。"""
    db = ctx.require("db")
    if not storage_sidecar.has_table(db):
        raise CheckSkipped("資料庫尚無側載表（資料庫版本較舊，尚未遷移）")
    return _to_result(storage_sidecar.orphans(db))


def sidecar_version_conflict_integrity(ctx: DoctorContext) -> CheckResult:
    """側載版本鎖（schema v18）：過期 `expected_version` 必須被拒。

    v18 前的庫為 skipped。"""
    db = ctx.require("db")
    if not storage_sidecar.has_table(db) or not storage_sidecar.has_version_column(db):
        raise CheckSkipped("資料庫尚無側載版本欄（資料庫版本較舊，尚未遷移）")
    return _to_result(storage_sidecar.version_conflict_integrity(db))


def tombstones_disjoint(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_manage.tombstones_disjoint(ctx.require("db")))


def notes_attribution(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_manage.note_attribution(ctx.require("db")))


def notes_principal_agreement(ctx: DoctorContext) -> CheckResult:
    """D12：設定的 principal（`LORE_VAULT_PRINCIPAL`）出現在既有 note 的 principal
    （含 updated_by_principal）之中。

    principal 預設由 UEPBernie 改為 owner：既有部署漏設 env 時，新寫入會記成另一個
    主體而完全看不出來。資料庫沒有 note 時通過；既有 note 的 principal 集合不含設定值
    為 warn（附既有值與要補的 env）。設定鍵 `principal` 由服務填；未提供時 skipped。
    """
    principal = ctx.settings.get("principal")
    if not principal:
        raise CheckSkipped("未提供 principal（不在服務內執行時用 --principal）")
    db = ctx.require("db")
    rows = db.execute(
        "SELECT principal AS p, count(*) AS n FROM notes "
        "WHERE principal IS NOT NULL GROUP BY principal "
        "UNION ALL "
        "SELECT updated_by_principal, count(*) FROM notes "
        "WHERE updated_by_principal IS NOT NULL GROUP BY updated_by_principal"
    ).fetchall()
    seen: dict[str, int] = {}
    for name, count in rows:
        seen[name] = seen.get(name, 0) + int(count)
    counts = {"principals": len(seen)}
    if not seen:
        return CheckResult.ok(f"尚無 note；principal 設定為 {principal}", counts=counts)
    if principal in seen:
        others = sorted(set(seen) - {principal})
        detail = f"（另有 {others}）" if others else ""
        return CheckResult.ok(
            f"principal {principal} 與既有 note 一致{detail}", counts=counts
        )
    existing = sorted(seen)
    return CheckResult.warn(
        f"設定的 principal {principal!r} 不在既有 note 的 principal {existing} 之中",
        details=[
            "新寫入會記成另一個主體；既有部署請在 .env 設 "
            f"LORE_VAULT_PRINCIPAL={existing[0]}（或既有的正確值）後重啟服務",
        ],
        counts=counts,
    )


def tombstones_note_snapshots(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_manage.tombstone_snapshots(ctx.require("db")))


def tombstones_summary(ctx: DoctorContext) -> CheckResult:
    """資訊項：墓碑數、快照總位元組、最舊一筆年齡。

    設定鍵 `tombstones_warn_age_days`／`tombstones_warn_bytes`（0 或未設 = 不警告）。
    """
    now = ctx.settings.get("now") or datetime.now(UTC)
    return _to_result(
        storage_manage.tombstone_stats(
            ctx.require("db"),
            now=now,
            warn_age_days=float(ctx.settings.get("tombstones_warn_age_days") or 0),
            warn_bytes=int(ctx.settings.get("tombstones_warn_bytes") or 0),
        )
    )


def ui_login_lock(ctx: DoctorContext) -> CheckResult:
    """A23：UI 登入鎖定中為 fail（附鎖定時間）；近 24 小時失敗次數為資訊。"""
    db = ctx.require("db")
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("ui_login_state",),
    ).fetchone()
    if exists is None:
        raise CheckSkipped("資料庫尚無 UI 登入表（資料庫版本較舊，尚未遷移）")
    now = ctx.settings.get("now") or datetime.now(UTC)
    return _to_result(storage_ui_login.lock_check(db, now=now))


def ask_provider(ctx: DoctorContext) -> CheckResult:
    """D11：`/v1/ask` 的問答模型用戶端是否建立（有沒有 OpenAI key）。

    設定鍵 `ask_configured`（bool，由服務依執行期狀態填）；未提供時 skipped
    （例如在服務外執行 doctor）。不打網路：模型名與 key 是否被接受要到實際呼叫才知道，
    錯誤會以 `ask_provider_error` 明確回給呼叫端。沒有 key 只影響 ask，所以是 warn。
    """
    if ctx.settings.get("ask_enabled") is False:
        raise CheckSkipped("問答已由管理者關閉（ask.enabled = false）")
    configured = ctx.settings.get("ask_configured")
    if configured is None:
        raise CheckSkipped("未提供 ask_configured（不在服務內執行）")
    model = ctx.settings.get("ask_model")
    if not configured:
        return CheckResult.warn(
            "未設定 OPENAI_API_KEY，ask 無法使用（會回 ask_not_configured）",
            details=[f"ask.model={model}"] if model else (),
        )
    return CheckResult.ok(f"ask 模型 {model}" if model else "")


# ── 執行期設定對帳（資源 "db"；D13，schema v15）──


def settings_overrides(ctx: DoctorContext) -> CheckResult:
    try:
        return _to_result(storage_settings.overrides_validity(ctx.require("db")))
    except storage_settings.MissingSettingsTables as exc:
        raise CheckSkipped(str(exc)) from None


def settings_audit_agreement(ctx: DoctorContext) -> CheckResult:
    try:
        return _to_result(storage_settings.audit_agreement(ctx.require("db")))
    except storage_settings.MissingSettingsTables as exc:
        raise CheckSkipped(str(exc)) from None


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
        (
            "storage.control_chars",
            storage_control_chars,
            "notes／concepts／episodes 沒有禁用的控制字元（NUL 等）或孤立 surrogate",
        ),
    ):
        registry.add(Check(name, "storage", func, description))
    for name, func, description in (
        (
            "space.valid_values",
            space_valid_values,
            "vault 的 space 都在白名單（dev／lore／personal）內",
        ),
        (
            "space.key_prefix_agreement",
            space_key_prefix_agreement,
            "非 dev 的 vault key／別名以 '<space>/' 開頭",
        ),
    ):
        registry.add(Check(name, "space", func, description))
    for name, func, description in (
        (
            "documents.blob_exists",
            documents_blob_exists,
            "document 引用的 blob 都存在且內容雜湊正確",
        ),
        (
            "documents.orphan_blobs",
            documents_orphan_blobs,
            "沒有 document 引用的 blob、不明檔案、遺留暫存檔（非零為 warn）",
        ),
        (
            "documents.chunk_count_matches",
            documents_chunk_count,
            "ready 文件的 chunk_count 與實際 chunk 數一致；非 ready 文件沒有 chunk",
        ),
        (
            "documents.fts_rows_match_chunks",
            documents_fts_rows,
            "chunk_fts 與可索引文件（ready、未被取代）的 chunk 一對一",
        ),
        (
            "documents.superseded_chunks_removed",
            documents_superseded_removed,
            "被取代或非 ready 的文件不在 FTS／向量索引內",
        ),
        (
            "documents.vector_rows_match_chunks",
            documents_vector_rows,
            "chunk 向量沒有孤兒與維度不符（fail）；可索引 chunk 缺向量為 warn",
        ),
        (
            "documents.stuck_processing",
            documents_stuck,
            "卡在 extracting 超過門檻的文件（fail：worker 中斷或沒在跑）",
        ),
        (
            "documents.failed",
            documents_failed,
            "抽取失敗與向量補算放棄的文件數（非零為 warn，附錯誤碼）",
        ),
        (
            "documents.quality_warnings",
            documents_quality_warnings,
            "ready 文件的抽取品質警示（例如 cp950 判定信心低；非零為 warn）",
        ),
        (
            "documents.backlog",
            documents_backlog,
            "待抽取文件與缺向量 chunk；最舊一筆等太久為 warn",
        ),
    ):
        registry.add(Check(name, "documents", func, description))
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
        (
            "enrich.queue_time",
            enrich_queue_time,
            "每則 note 都有補算入列時間（缺少會讓積壓等待時間少算）",
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
    registry.add(
        Check(
            "vaults.misc_routing",
            "vaults",
            vaults_misc_routing,
            "雜項 vault 唯一且無別名、episode 的 origin_key 與歸屬一致、"
            "不再有收料自動建立的 folder vault（D14）",
        )
    )
    registry.add(
        Check(
            "vaults.alias_integrity",
            "vaults",
            vaults_alias_integrity,
            "別名不等於任何 vault 的正式 key，且指向現存 vault",
        )
    )
    registry.add(
        Check(
            "sidecar.orphans",
            "sidecar",
            sidecar_orphans,
            "側載列指向現存 vault 且 space 相符（vault 刪除／換 space 須同交易處理）",
        )
    )
    registry.add(
        Check(
            "sidecar.version_conflict_integrity",
            "sidecar",
            sidecar_version_conflict_integrity,
            "側載帶過期 expected_version 的寫入被拒並附目前內容，不默默覆寫",
        )
    )
    registry.add(
        Check(
            "tombstones.disjoint",
            "tombstones",
            tombstones_disjoint,
            "note／文件墓碑與現行表沒有重複 id（undelete 須同交易刪墓碑）",
        )
    )
    registry.add(
        Check(
            "tombstones.note_snapshots",
            "tombstones",
            tombstones_note_snapshots,
            "note 墓碑的內容快照可解析、id 相符、還原必要欄位齊全",
        )
    )
    registry.add(
        Check(
            "tombstones.summary",
            "tombstones",
            tombstones_summary,
            "資訊：墓碑數、快照總位元組、最舊一筆年齡（設了門檻才會 warn）",
        )
    )
    registry.add(
        Check(
            "ui.login_lock",
            "ui",
            ui_login_lock,
            "UI 登入未被鎖定（鎖定為 fail，需人工 ui-unlock）；近 24 小時失敗次數",
        )
    )
    registry.add(
        Check(
            "notes.attribution",
            "notes",
            notes_attribution,
            "每則 note 都記錄了寫入者與最後修改者的帳號",
        )
    )
    registry.add(
        Check(
            "notes.principal_agreement",
            "notes",
            notes_principal_agreement,
            "設定的 principal（LORE_VAULT_PRINCIPAL）出現在既有 note 的 principal 中"
            "（不一致為 warn）",
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
    registry.add(
        Check(
            "concept_snapshot.path_agreement",
            "concept_snapshot",
            concept_snapshot_path_agreement,
            "client.env 的 LORE_VAULT_CONCEPT_SNAPSHOT 與 MCP 快照路徑指向同一檔",
        )
    )
    for name, func, description in (
        (
            "settings.overrides",
            settings_overrides,
            "設定頁存的覆寫都在白名單內、型別與範圍合法（不合法的會被略過、未生效）",
        ),
        (
            "settings.audit_agreement",
            settings_audit_agreement,
            "設定覆寫與稽核紀錄一致（每次修改都留下誰、何時、舊值→新值）",
        ),
    ):
        registry.add(Check(name, "settings", func, description))
    registry.add(
        Check(
            "ask.provider",
            "ask",
            ask_provider,
            "ask 的問答模型用戶端已建立（有 OpenAI key；缺少為 warn）",
        )
    )
    registry.add(
        Check(
            "concept_push.lag",
            "concept_push",
            concept_push_lag,
            "主機 concepts.json 的 id 都已推送到服務、上次推送沒有失敗",
        )
    )
    registry.add(
        Check(
            "episode_pull.status",
            "episode_pull",
            episode_pull_status,
            "管線上次從服務拉取 episode 成功、快取與服務端筆數一致、服務端沒有少資料",
        )
    )
    registry.add(
        Check(
            "concept_scope.anchor_agreement",
            "concept_scope",
            concept_scope_anchor_agreement,
            "concept 的 scope 與自己的 anchors 一致，不是蒸餾時猜錯的上位名稱",
        )
    )
    return registry
