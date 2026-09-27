"""spike 資料路徑的唯一來源（T-10）。

資料目錄刻意放在 repo 外（理由見 hook_stop.py 的 DEFAULT_EPISODE_DIR 註解）。
預設 ``~/.lore-vault/``（D5，2026-09-27 由 ``~/.claude/agent-memory-spike/`` 改名；
與 MCP 殼同目錄，殼只用 ``mcp.toml``／``mcp.env``／``snapshot/``／``venv/``，檔名不衝突）。

過渡 fallback：新目錄還沒有 ``episodes/``、舊目錄有時，整個根目錄解析到舊位置。
以整根為單位切換，不逐檔 fallback——逐檔會讓 Stop hook 寫新、管線讀舊，資料分裂。
搬移時 ``episodes/`` 一落地到新目錄，所有模組下次 import 就一起切過去。
fallback 生效或兩邊都有 ``episodes/`` 時由 ``hook_health_alert`` 告警。

``LORE_VAULT_SPIKE_HOME`` 可覆寫根目錄（不套 fallback），給測試隔離與日後搬遷用。
所有常數在 import 時定值。

hook 路徑會直接 import 本模組：只用標準庫，不 import 任何同目錄模組。
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "LORE_VAULT_SPIKE_HOME"


# D5 改名前的舊位置，只在過渡 fallback 與健康告警用
LEGACY_WORK_DIR = Path.home() / ".claude" / "agent-memory-spike"
DEFAULT_WORK_DIR = Path.home() / ".lore-vault"
# 判斷「這裡有 spike 資料」的標記；Stop hook 每輪都寫這裡，是最不會缺的一項
DATA_MARKER = "episodes"


def _resolve_work_dir() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    if (
        not (DEFAULT_WORK_DIR / DATA_MARKER).is_dir()
        and (LEGACY_WORK_DIR / DATA_MARKER).is_dir()
    ):
        return LEGACY_WORK_DIR
    return DEFAULT_WORK_DIR


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

# 階段 8（spike 接入服務）：hook 端客戶端設定（服務位址、token、concept 快照路徑），
# 可用 LORE_VAULT_CLIENT_ENV 改位置。episode spool 在 episode 目錄的同層 `spool/`
# （預設即 WORK_DIR / "spool"），佈局見 src/lore_vault/hooks/spool.py
CLIENT_ENV_PATH = WORK_DIR / "client.env"
