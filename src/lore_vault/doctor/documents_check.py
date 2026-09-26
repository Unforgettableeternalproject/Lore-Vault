"""文件 blob 對帳（T-59）：`documents.blob_exists`、`documents.orphan_blobs`。

資源 `db`：sqlite3 連線（缺 → skipped）。
設定鍵：
- `blob_dir`：blob 目錄；未設時載入設定（`config`：設定檔路徑，未給則依
  `LORE_VAULT_CONFIG`／環境變數）取 `documents.blob_dir`——容器內只給 `--db`
  就會用映像設定的 `/data/blobs`。兩者皆無 → skipped
- `now`：datetime，測試注入用（判斷暫存檔是否為中斷遺留）
"""

from __future__ import annotations

from datetime import datetime

from lore_vault.config import load_config
from lore_vault.storage import blobs as storage_blobs

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
