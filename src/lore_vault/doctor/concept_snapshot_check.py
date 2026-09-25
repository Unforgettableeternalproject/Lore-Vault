"""concept 快照對帳（T-40）：PreToolUse 讀的快照檔是否新鮮、完整。

設定鍵：
- `concept_snapshot`：快照檔路徑（未設 → skipped）
- `concept_snapshot_max_age_hours`：年齡門檻（小時），預設 24；
  以 manifest `checked_at` 計
- `now`：datetime，測試注入用
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lore_vault.hooks import concept_snapshot

from .framework import CheckResult, CheckSkipped, DoctorContext

DEFAULT_MAX_AGE_HOURS = 24.0


def concept_snapshot_age(ctx: DoctorContext) -> CheckResult:
    value = ctx.settings.get("concept_snapshot")
    if not value:
        raise CheckSkipped("缺少設定：concept_snapshot")
    hours = float(
        ctx.settings.get("concept_snapshot_max_age_hours", DEFAULT_MAX_AGE_HOURS)
    )
    now = ctx.settings.get("now") or datetime.now(UTC)
    check = concept_snapshot.check_age(
        Path(str(value)), now=now, max_age_seconds=hours * 3600
    )
    counts = {}
    if check.concepts is not None:
        counts["concepts"] = check.concepts
    if check.age_seconds is not None:
        counts["age_seconds"] = int(check.age_seconds)
    factory = CheckResult.ok if check.status == "pass" else CheckResult.fail
    return factory(check.summary, counts=counts)
