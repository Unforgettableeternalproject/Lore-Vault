"""episode spool 對帳（T-38）：客戶端可獨立執行，不需要連服務。

設定鍵：
- `spool_dir`：spool 目錄（未設 → skipped）；
  spike 預設 `~/.claude/agent-memory-spike/spool`
- `client_env`：hook 端設定檔（判斷「推送未設定」）；未設時用 `spool_dir` 上一層的
  `client.env`（與 spike `paths.CLIENT_ENV_PATH` 同位置）。只讀檔案，不看行程環境變數
- `spool_warn_age_hours`（預設 1）、`spool_fail_age_hours`（預設 24）：
  最舊一筆待推送的年齡門檻
- `now`：datetime，測試注入用
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lore_vault.hooks import spool
from lore_vault.hooks.client_env import load_client_settings

from .framework import CheckResult, CheckSkipped, DoctorContext

DEFAULT_WARN_AGE_HOURS = 1.0
DEFAULT_FAIL_AGE_HOURS = 24.0
CLIENT_ENV_NAME = "client.env"


def _spool_dir(ctx: DoctorContext) -> Path:
    value = ctx.settings.get("spool_dir")
    if not value:
        raise CheckSkipped("缺少設定：spool_dir")
    return Path(str(value))


def _now(ctx: DoctorContext) -> datetime:
    return ctx.settings.get("now") or datetime.now(UTC)


def spool_pending(ctx: DoctorContext) -> CheckResult:
    spool_dir = _spool_dir(ctx)
    stats = spool.spool_stats(spool_dir, now=_now(ctx))
    warn_h = float(ctx.settings.get("spool_warn_age_hours", DEFAULT_WARN_AGE_HOURS))
    fail_h = float(ctx.settings.get("spool_fail_age_hours", DEFAULT_FAIL_AGE_HOURS))
    env_file = ctx.settings.get("client_env") or spool_dir.parent / CLIENT_ENV_NAME
    client = load_client_settings(Path(str(env_file)), environ={})

    age = stats.oldest_pending_age
    state = spool.load_push_state(spool_dir)
    counts = {
        "pending": stats.pending,
        "oldest_age_seconds": int(age) if age is not None else 0,
    }
    details = [client.describe()]
    if state.get("last_error"):
        details.append(f"上次推送失敗：{state['last_error']}")
    if state.get("last_ok_at"):
        details.append(f"上次推送成功：{state['last_ok_at']}")

    if state.get("last_error_kind") == spool.ERROR_KIND_DISABLED:
        # 服務端刻意關閉收料（D13）：資料安全留在本機，不因年齡升成 fail
        return CheckResult.warn(
            f"服務未開啟 episode 收料：{stats.pending} 筆留在本機，服務開啟後自動補推",
            details=details,
            counts=counts,
        )
    if age is not None and age > fail_h * 3600:
        return CheckResult.fail(
            f"{stats.pending} 筆未推送，最舊 {age / 3600:.1f} 小時（門檻 {fail_h:g}）",
            details=details,
            counts=counts,
        )
    if age is not None and age > warn_h * 3600:
        return CheckResult.warn(
            f"{stats.pending} 筆未推送，最舊 {age / 3600:.1f} 小時（門檻 {warn_h:g}）",
            details=details,
            counts=counts,
        )
    if not client.push_configured:
        return CheckResult.warn(
            f"推送未設定（{stats.pending} 筆待推送）", details=details, counts=counts
        )
    return CheckResult.ok(f"{stats.pending} 筆待推送", details=details, counts=counts)


def spool_conflicts(ctx: DoctorContext) -> CheckResult:
    spool_dir = _spool_dir(ctx)
    stats = spool.spool_stats(spool_dir, now=_now(ctx))
    counts = {"rejected": stats.rejected, **stats.rejected_by_status}
    if stats.rejected:
        return CheckResult.fail(
            f"{stats.rejected} 筆被拒收或損毀，留在 {spool_dir / spool.REJECTED}",
            details=[f"{k}: {v}" for k, v in sorted(stats.rejected_by_status.items())],
            counts=counts,
        )
    return CheckResult.ok(counts=counts)
