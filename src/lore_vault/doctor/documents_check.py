"""文件 blob 對帳（T-59）：`documents.blob_exists`、`documents.orphan_blobs`。

資源 `db`：sqlite3 連線（缺 → skipped）。
設定鍵：
- `blob_dir`：blob 目錄；未設時載入設定（`config`：設定檔路徑，未給則依
  `LORE_VAULT_CONFIG`／環境變數）取 `documents.blob_dir`——容器內只給 `--db`
  就會用映像設定的 `/data/blobs`。兩者皆無 → skipped
- `now`：datetime，測試注入用（判斷暫存檔是否為中斷遺留）
"""

from __future__ import annotations

from datetime import UTC, datetime

from lore_vault.config import load_config
from lore_vault.storage import blobs as storage_blobs
from lore_vault.storage import document_index

from .framework import CheckResult, CheckSkipped, DoctorContext


def _store(ctx: DoctorContext) -> storage_blobs.BlobStore:
    value = ctx.settings.get("blob_dir")
    if not value:
        value = load_config(
            ctx.settings.get("config"), environ=ctx.settings.get("environ")
        ).documents.blob_dir
    if not value:
        raise CheckSkipped("缺少設定：blob_dir（或設定檔的 documents.blob_dir）")
    return storage_blobs.BlobStore(str(value))


def _to_result(rec: storage_blobs.Reconciliation) -> CheckResult:
    factory = {
        "pass": CheckResult.ok,
        "warn": CheckResult.warn,
        "fail": CheckResult.fail,
    }[rec.status]
    return factory(rec.summary, details=rec.details, counts=rec.counts)


def documents_blob_exists(ctx: DoctorContext) -> CheckResult:
    db = ctx.require("db")
    return _to_result(storage_blobs.blob_exists(db, _store(ctx)))


def documents_orphan_blobs(ctx: DoctorContext) -> CheckResult:
    db = ctx.require("db")
    now = ctx.settings.get("now")
    moment = now.timestamp() if isinstance(now, datetime) else None
    return _to_result(storage_blobs.orphan_blobs(db, _store(ctx), now=moment))


# ── chunk 索引與 worker 對帳（T-63～T-67；資源 db）──────────────────────
# 設定鍵：`embedding_dim`（向量維度；沒有就不檢查維度）、`now`（datetime）、
# `documents_stuck_seconds`（extracting 逾時，預設 3600）、
# `documents_backlog_max_age`（最舊待處理等待秒數，預設 3600）

DEFAULT_STUCK_SECONDS = 3600.0
DEFAULT_DOCUMENT_BACKLOG_MAX_AGE = 3600.0


def _db(ctx: DoctorContext):
    db = ctx.require("db")
    has = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
    ).fetchone()
    if has is None:
        raise CheckSkipped("缺少 documents 表（schema 未遷移）")
    return db


def _now(ctx: DoctorContext) -> datetime:
    now = ctx.settings.get("now")
    return now if isinstance(now, datetime) else datetime.now(UTC)


def documents_chunk_count(ctx: DoctorContext) -> CheckResult:
    return _to_result(document_index.chunk_count_matches(_db(ctx)))


def documents_fts_rows(ctx: DoctorContext) -> CheckResult:
    return _to_result(document_index.fts_rows_match_chunks(_db(ctx)))


def documents_superseded_removed(ctx: DoctorContext) -> CheckResult:
    return _to_result(document_index.superseded_chunks_removed(_db(ctx)))


def documents_vector_rows(ctx: DoctorContext) -> CheckResult:
    dim = ctx.settings.get("embedding_dim")
    return _to_result(
        document_index.vector_rows_match_chunks(
            _db(ctx), dim=None if dim is None else int(dim)
        )
    )


def documents_stuck(ctx: DoctorContext) -> CheckResult:
    seconds = float(ctx.settings.get("documents_stuck_seconds", DEFAULT_STUCK_SECONDS))
    return _to_result(
        document_index.stuck_processing(
            _db(ctx), now=_now(ctx), max_age_seconds=seconds
        )
    )


def documents_failed(ctx: DoctorContext) -> CheckResult:
    return _to_result(document_index.failed_documents(_db(ctx)))


def documents_quality_warnings(ctx: DoctorContext) -> CheckResult:
    db = _db(ctx)
    has = any(
        row[1] == "warnings"
        for row in db.execute("PRAGMA table_info(documents)").fetchall()
    )
    if not has:
        raise CheckSkipped("缺少 documents.warnings 欄（schema 未遷移到 v10）")
    return _to_result(document_index.quality_warnings(db))


def documents_backlog(ctx: DoctorContext) -> CheckResult:
    max_age = float(
        ctx.settings.get("documents_backlog_max_age", DEFAULT_DOCUMENT_BACKLOG_MAX_AGE)
    )
    return _to_result(
        document_index.backlog(_db(ctx), now=_now(ctx), max_age_seconds=max_age)
    )
