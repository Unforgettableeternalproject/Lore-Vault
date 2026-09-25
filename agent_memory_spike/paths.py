"""spike 資料路徑的唯一來源（T-10）。

資料目錄刻意放在 repo 外（理由見 hook_stop.py 的 DEFAULT_EPISODE_DIR 註解）。
預設值維持現行的 ``~/.claude/agent-memory-spike/``；是否改名、中間產物去留待 D5
定案（T-46），屆時只改這裡。

``LORE_VAULT_SPIKE_HOME`` 可覆寫根目錄，給測試隔離與日後搬遷用。
所有常數在 import 時定值，行為與集中前各模組自己拼路徑時相同。

hook 路徑會直接 import 本模組：只用標準庫，不 import 任何同目錄模組。
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "LORE_VAULT_SPIKE_HOME"


def _resolve_work_dir() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "agent-memory-spike"


# 資料根目錄
WORK_DIR = _resolve_work_dir()

# 多個模組共用的資料位置
EPISODE_DIR = WORK_DIR / "episodes"
INJECTION_LOG = WORK_DIR / "injections.jsonl"
TOUCH_LOG = WORK_DIR / "touches.jsonl"
CONCEPT_PATH = WORK_DIR / "concepts.json"
INJECT_STATE_DIR = WORK_DIR / "inject_state"
LOG_DIR = WORK_DIR / "logs"
PIPELINE_STATE_PATH = WORK_DIR / "pipeline_state.json"
PIPELINE_LOCK_PATH = WORK_DIR / "pipeline.lock"
