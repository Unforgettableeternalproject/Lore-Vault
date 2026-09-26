"""concept 快照對帳（T-40）：PreToolUse 讀的快照檔是否新鮮、完整。

設定鍵：
- `concept_snapshot`：快照檔路徑（未設 → skipped）
- `concept_snapshot_max_age_hours`：年齡門檻（小時），預設 24；
  以 manifest `checked_at` 計
- `now`：datetime，測試注入用

`concept_snapshot.path_agreement` 另用：
- `client_env`：hook 端設定檔；未設時用 `spool_dir` 上一層的 `client.env`
  （同 spool 對帳）。兩者皆無 → skipped。只讀檔案，不看行程環境變數
- `mcp_concept_snapshot_path`：MCP 殼拉快照寫入的位置；未設時載入設定
  （`config`：設定檔路徑，未給則依 `LORE_VAULT_CONFIG`／環境變數），取
  `mcp.concept_snapshot_path`，未設則 `<mcp.snapshot_dir>/concepts.json`
  （與殼相同的派生）
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from lore_vault.config import load_config
from lore_vault.hooks import concept_snapshot
from lore_vault.hooks.client_env import CONCEPT_SNAPSHOT_KEY, load_client_settings

from .framework import CheckResult, CheckSkipped, DoctorContext
from .spool_check import CLIENT_ENV_NAME

DEFAULT_MAX_AGE_HOURS = 24.0
# 與 lore_vault.mcp.settings.CONCEPT_SNAPSHOT_NAME 相同；
# 不 import mcp，免得 doctor 拖進 HTTP 依賴
MCP_CONCEPT_SNAPSHOT_NAME = "concepts.json"


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


def _client_env_file(ctx: DoctorContext) -> Path:
    value = ctx.settings.get("client_env")
    if value:
        return Path(str(value))
    spool_dir = ctx.settings.get("spool_dir")
    if spool_dir:
        return Path(str(spool_dir)).parent / CLIENT_ENV_NAME
    raise CheckSkipped("缺少設定：client_env（或 spool_dir）")


def _mcp_concept_path(ctx: DoctorContext) -> Path:
    value = ctx.settings.get("mcp_concept_snapshot_path")
    if value:
        return Path(str(value)).expanduser()
    mcp = load_config(
        ctx.settings.get("config"), environ=ctx.settings.get("environ")
    ).mcp
    if mcp.concept_snapshot_path:
        return Path(mcp.concept_snapshot_path).expanduser()
    if mcp.snapshot_dir:
        return Path(mcp.snapshot_dir).expanduser() / MCP_CONCEPT_SNAPSHOT_NAME
    raise CheckSkipped(
        "MCP 未設定 concept 快照路徑（mcp.concept_snapshot_path／snapshot_dir）"
    )


def _same_file(left: Path, right: Path) -> bool:
    # Windows 路徑大小寫不敏感；resolve(strict=False) 讓不存在的檔也能比
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )


def concept_snapshot_path_agreement(ctx: DoctorContext) -> CheckResult:
    """PreToolUse 讀的快照（client.env）必須就是 MCP 殼拉下來寫入的那一個。

    兩邊各設各的時，MCP 照常拉取、`concept_snapshot.age` 也可能是綠的，
    但 hook 讀的是另一個（從未更新或根本不存在）的檔——注入靜默失效。
    """
    env_file = _client_env_file(ctx)
    client = load_client_settings(env_file, environ={})
    if client.concept_snapshot is None:
        raise CheckSkipped(
            f"{env_file} 未設 {CONCEPT_SNAPSHOT_KEY}（沿用現行 concepts.json）"
        )
    mcp_path = _mcp_concept_path(ctx)
    details = [f"client.env：{client.concept_snapshot}", f"MCP：{mcp_path}"]
    if _same_file(client.concept_snapshot, mcp_path):
        return CheckResult.ok("hook 與 MCP 指向同一個快照", details=details)
    return CheckResult.fail(
        "hook 讀的快照與 MCP 拉取寫入的不是同一檔（PreToolUse 讀不到新快照）",
        details=details,
    )
