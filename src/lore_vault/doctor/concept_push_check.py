"""concept 推送對帳：服務端 concept 是否落後主機本地的 `concepts.json`。

排程在 `pipeline.py --run` 成功後跑 `--push-concepts`，成功時把推過的 id 寫進
`pipeline_state.json` 的 `service_pushed_concept_ids`；每次實推（成敗）
寫 `concept_push`。
PreToolUse 改讀 MCP 從服務拉的快照後，推送停掉＝注入內容凍結，而快照年齡
（以 `checked_at` 計）照樣是綠的；這項用既有的已推送 id 紀錄比對，不連服務。

設定鍵：
- `spike_home`：spike 資料目錄（含 `concepts.json`、`pipeline_state.json`）；
  未設時用 `spool_dir` 上一層（spool 預設在資料目錄下）。兩者皆無 → skipped
- `concept_push_grace_seconds`：`concepts.json` 修改時間晚於上次成功推送
  多少秒才 warn（預設 60）

判定：
- `concepts.json` 不存在 → skipped；讀不出來或不是陣列 → fail
- 上次實推失敗 → fail
- 從未推送（沒有已推送 id）→ warn
- 本地有未推送 id，或已推送 id 本地已不存在（待刪除）→ fail
- id 一致但 `concepts.json` 在上次成功推送後又被改過（內容可能未推）→ warn
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .framework import CheckResult, CheckSkipped, DoctorContext

# 與 agent_memory_spike/pipeline.py、paths.py 相同（src 不 import spike；
# agent_memory_spike/test_pipeline.py 驗證兩邊一致）
CONCEPTS_NAME = "concepts.json"
PIPELINE_STATE_NAME = "pipeline_state.json"
PUSHED_IDS_KEY = "service_pushed_concept_ids"
CONCEPT_PUSH_KEY = "concept_push"
DEFAULT_GRACE_SECONDS = 60.0
SAMPLE_LIMIT = 5


def _spike_home(ctx: DoctorContext) -> Path:
    value = ctx.settings.get("spike_home")
    if value:
        return Path(str(value)).expanduser()
    spool_dir = ctx.settings.get("spool_dir")
    if spool_dir:
        return Path(str(spool_dir)).expanduser().parent
    raise CheckSkipped("缺少設定：spike_home（或 spool_dir）")


def _sample(ids: set[str]) -> str:
    items = sorted(ids)
    more = f" …等 {len(items)} 筆" if len(items) > SAMPLE_LIMIT else ""
    return ", ".join(items[:SAMPLE_LIMIT]) + more


def _push_detail(record: dict[str, Any]) -> str:
    status = "成功" if record.get("ok") else "失敗"
    return f"上次推送：{record.get('at')}（{status}）{record.get('summary') or ''}"


def concept_push_lag(ctx: DoctorContext) -> CheckResult:
    home = _spike_home(ctx)
    concepts_path = home / CONCEPTS_NAME
    if not concepts_path.exists():
        raise CheckSkipped(f"{concepts_path} 不存在")
    try:
        concepts = json.loads(concepts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult.fail(f"{CONCEPTS_NAME} 讀不出來：{type(exc).__name__}")
    if not isinstance(concepts, list):
        return CheckResult.fail(f"{CONCEPTS_NAME} 不是 concept 陣列")
    local = {
        c["id"]
        for c in concepts
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]
    }

    state_path = home / PIPELINE_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        return CheckResult.fail(f"{PIPELINE_STATE_NAME} 讀不出來：{type(exc).__name__}")
    if not isinstance(state, dict):
        return CheckResult.fail(f"{PIPELINE_STATE_NAME} 不是物件")

    record = state.get(CONCEPT_PUSH_KEY)
    record = record if isinstance(record, dict) else None
    details = [_push_detail(record)] if record else []
    counts = {"local": len(local)}

    pushed_raw = state.get(PUSHED_IDS_KEY)
    if record and record.get("ok") is False:
        return CheckResult.fail(
            "上次 concept 推送失敗，服務端停在更早的成功推送",
            details=details,
            counts=counts,
        )
    if not isinstance(pushed_raw, list):
        return CheckResult.warn(
            f"從未推送 concept 到服務（{PIPELINE_STATE_NAME} 沒有 {PUSHED_IDS_KEY}）",
            details=details,
            counts=counts,
        )

    pushed = {str(i) for i in pushed_raw}
    unpushed = local - pushed
    pending_delete = pushed - local
    counts.update(
        pushed=len(pushed), unpushed=len(unpushed), pending_delete=len(pending_delete)
    )
    if unpushed or pending_delete:
        if unpushed:
            details.append(f"未推送：{_sample(unpushed)}")
        if pending_delete:
            details.append(f"待刪除：{_sample(pending_delete)}")
        return CheckResult.fail(
            f"服務端 concept 落後本地：{len(unpushed)} 筆未推送、"
            f"{len(pending_delete)} 筆待刪除（跑 pipeline.py --push-concepts）",
            details=details,
            counts=counts,
        )

    at = _parse_at(record.get("at")) if record else None
    if at is not None:
        grace = float(
            ctx.settings.get("concept_push_grace_seconds", DEFAULT_GRACE_SECONDS)
        )
        modified = concepts_path.stat().st_mtime
        if modified > at.timestamp() + grace:
            return CheckResult.warn(
                f"id 一致，但 {CONCEPTS_NAME} 在上次成功推送後有變動（內容可能未推送）",
                details=details,
                counts=counts,
            )
    return CheckResult.ok(
        f"{len(local)} 筆 id 皆已推送", details=details, counts=counts
    )


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 沒帶時區的舊紀錄無法和 mtime 比，當作沒有時間
    return parsed if parsed.tzinfo is not None else None
