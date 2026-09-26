"""快照對帳（T-31）：MCP 殼端可獨立執行，不需要連服務。

設定鍵：
- `snapshot_dir`：快照目錄（未設 → skipped）
- `snapshot_max_age_hours`：快照年齡門檻（小時），預設 24
- `now`：datetime，測試注入用
"""

from __future__ import annotations

from datetime import UTC, datetime

from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.checks import Reconciliation

from .framework import CheckResult, CheckSkipped, DoctorContext

DEFAULT_SNAPSHOT_MAX_AGE_HOURS = 24.0


def _to_result(rec: Reconciliation) -> CheckResult:
    factory = CheckResult.ok if rec.status == "pass" else CheckResult.fail
    return factory(rec.summary, details=rec.details, counts=rec.counts)


def _snapshot_dir(ctx: DoctorContext) -> str:
    snapshot_dir = ctx.settings.get("snapshot_dir")
    if not snapshot_dir:
        raise CheckSkipped("缺少設定：snapshot_dir")
    return str(snapshot_dir)


def snapshot_schema(ctx: DoctorContext) -> CheckResult:
    return _to_result(storage_snapshot.snapshot_schema(_snapshot_dir(ctx)))


def snapshot_age(ctx: DoctorContext) -> CheckResult:
    snapshot_dir = _snapshot_dir(ctx)
    now = ctx.settings.get("now") or datetime.now(UTC)
    hours = float(
        ctx.settings.get("snapshot_max_age_hours", DEFAULT_SNAPSHOT_MAX_AGE_HOURS)
    )
    return _to_result(
        storage_snapshot.snapshot_age(
            snapshot_dir, now=now, max_age_seconds=hours * 3600
        )
    )
