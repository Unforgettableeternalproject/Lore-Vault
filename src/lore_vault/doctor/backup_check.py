"""備份對帳（T-27）：`backup.recent`。

設定鍵：
- `backup_dir`：備份目錄（未設 → skipped）
- `backup_max_age_hours`：門檻（小時），預設 26；超過或從未備份為 fail
- `now`：datetime，測試注入用
"""

from __future__ import annotations

from datetime import UTC, datetime

from lore_vault.storage.backup import backup_freshness

from .framework import CheckResult, CheckSkipped, DoctorContext

DEFAULT_BACKUP_MAX_AGE_HOURS = 26.0


def backup_recent(ctx: DoctorContext) -> CheckResult:
    backup_dir = ctx.settings.get("backup_dir")
    if not backup_dir:
        raise CheckSkipped("缺少設定：backup_dir")
    now = ctx.settings.get("now") or datetime.now(UTC)
    hours = float(
        ctx.settings.get("backup_max_age_hours", DEFAULT_BACKUP_MAX_AGE_HOURS)
    )
    rec = backup_freshness(backup_dir, now=now, max_age_seconds=hours * 3600)
    factory = CheckResult.ok if rec.status == "pass" else CheckResult.fail
    return factory(rec.summary, details=rec.details, counts=rec.counts)
