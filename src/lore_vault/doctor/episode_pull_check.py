"""episode 拉取對帳（D13）：主機管線的蒸餾語料是否真的涵蓋服務端全部機器的 episode。

`pipeline.py` 的 `pull` 階段每次實跑都把結果寫進 `pipeline_state.json` 的
`episode_pull`（細節見 `agent_memory_spike/episode_source.py`）。拉取失敗時管線退回
本機 jsonl 照跑、階段仍報 OK——遠端 episode 靜默地沒進蒸餾，只有這筆紀錄看得到。
這項只讀紀錄，不連服務。

設定鍵：
- `spike_home`：spike 資料目錄（同 `concept_push.lag`；未設時用 `spool_dir` 上一層）
- `episode_pull_stale_days`：距上次**成功**從服務拉取超過幾天就 fail（預設 3）
- `now`：datetime，測試注入用

判定：
- 沒有紀錄 → warn（還沒跑過含 pull 階段的管線）
- `mode=failed`（`--on-pull-failure fail` 且拉取失敗）→ fail
- 服務端缺少快取中曾收下的 episode（`server_missing > 0`）→ fail
- 成功拉取但快取中服務端仍有的筆數 ≠ 服務端 total → fail
- 降級（`local_fallback`）：對帳錯誤（`consistency`）或距上次成功超過門檻 → fail，
  否則 warn
- 強制本機（`local_forced`）→ warn
- 舊版服務（不支援增量水位）→ warn
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .concept_push_check import PIPELINE_STATE_NAME, _spike_home
from .framework import CheckResult, CheckSkipped, DoctorContext

# 與 agent_memory_spike/episode_source.py 相同（src 不 import spike；
# agent_memory_spike/test_episode_source.py 驗證兩邊一致）
EPISODE_PULL_KEY = "episode_pull"
MODE_SERVICE = "service"
MODE_FALLBACK = "local_fallback"
MODE_FORCED = "local_forced"
MODE_FAILED = "failed"
ERROR_CONSISTENCY = "consistency"
DEFAULT_STALE_DAYS = 3.0


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def episode_pull_status(ctx: DoctorContext) -> CheckResult:
    home = _spike_home(ctx)
    state_path = home / PIPELINE_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CheckSkipped(f"{state_path} 不存在") from None
    except (OSError, ValueError) as exc:
        return CheckResult.fail(f"{PIPELINE_STATE_NAME} 讀不出來：{type(exc).__name__}")
    if not isinstance(state, dict):
        return CheckResult.fail(f"{PIPELINE_STATE_NAME} 不是物件")

    record = state.get(EPISODE_PULL_KEY)
    if not isinstance(record, dict):
        return CheckResult.warn(
            "管線還沒從服務拉取過 episode（pipeline_state.json 沒有 episode_pull）"
        )

    raw_counts = record.get("counts")
    counts = {
        k: v
        for k, v in (raw_counts.items() if isinstance(raw_counts, dict) else [])
        if isinstance(v, int) and not isinstance(v, bool)
    }
    mode = record.get("mode")
    details = [f"上次：{record.get('at')}（{mode}）"]
    if record.get("reason"):
        details.append(f"原因：{record['reason']}")
    if record.get("resync_reason"):
        details.append(f"全量重拉：{record['resync_reason']}")
    if record.get("last_ok_at"):
        details.append(f"上次成功從服務拉取：{record['last_ok_at']}")

    now = ctx.settings.get("now") or datetime.now(UTC)
    stale_days = float(ctx.settings.get("episode_pull_stale_days", DEFAULT_STALE_DAYS))
    last_ok = _parse(record.get("last_ok_at"))
    stale = last_ok is None or (now - last_ok).total_seconds() > stale_days * 86400

    if mode == MODE_FAILED:
        return CheckResult.fail(
            "上次從服務拉取 episode 失敗，管線停止", details=details, counts=counts
        )
    missing = counts.get("server_missing", 0)
    if missing > 0:
        sample = record.get("server_missing_sample") or []
        details.append(f"缺少（session_id, prompt_id, turn_index）樣本：{sample[:5]}")
        details.append(
            "處理：確認服務端資料庫是否被還原；本機的仍在 jsonl，可重推 spool 或"
            "刪除快取目錄 episode_cache/ 重建"
        )
        return CheckResult.fail(
            f"服務端缺少 {missing} 筆曾經收下的 episode", details=details, counts=counts
        )
    if mode == MODE_SERVICE:
        total = record.get("service_total")
        cached = counts.get("cached")
        if isinstance(total, int) and isinstance(cached, int) and cached != total:
            return CheckResult.fail(
                f"快取 {cached} 筆與服務端 {total} 筆不符",
                details=details,
                counts=counts,
            )
        if record.get("legacy"):
            return CheckResult.warn(
                "服務端不支援增量水位（舊版），每輪都全量拉取、無法對帳筆數",
                details=details,
                counts=counts,
            )
        return CheckResult.ok(
            f"已從服務拉取 episode（快取 {counts.get('cached', 0)} 筆，"
            f"合併 {counts.get('merged', 0)} 筆）",
            details=details,
            counts=counts,
        )
    if mode == MODE_FALLBACK:
        if record.get("error_kind") == ERROR_CONSISTENCY:
            return CheckResult.fail(
                "從服務拉取的筆數對不上，本輪降級為本機語料",
                details=details,
                counts=counts,
            )
        if stale:
            return CheckResult.fail(
                f"已超過 {stale_days:g} 天沒有成功從服務拉取 episode"
                "（持續降級為本機語料）",
                details=details,
                counts=counts,
            )
        return CheckResult.warn(
            "上次從服務拉取失敗，降級為本機語料（遠端 episode 本輪沒進蒸餾）",
            details=details,
            counts=counts,
        )
    if mode == MODE_FORCED:
        return CheckResult.warn(
            "管線被設成只讀本機 episode（--episode-source local）",
            details=details,
            counts=counts,
        )
    return CheckResult.fail(f"episode_pull 的 mode 無法辨識：{mode!r}", details=details)
