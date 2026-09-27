"""執行期有效設定（D13）：設定檔／環境變數的 Config 套上 DB 覆寫，帶程序內快取。

- 讀取端（episode 收料、ask、`/v1/status` 的 doctor 門檻、登入紀錄保留、HTTP MCP 下載
  上限）每次使用都呼叫 `current()`，不在啟動時把值抄走
- 快取在設定 API 寫入成功（交易 commit 後）立即失效。依據 A9：服務是 DB 的唯一寫入程序，
  覆寫只經本程序的設定 API 寫入；doctor CLI 等其他程序不經快取、直接讀 DB
- 讀取失敗（例如遷移前表不存在）時退回純設定檔的值，不快取
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager

from lore_vault.config import Config, ConfigError
from lore_vault.runtime_settings import InvalidSetting, apply_overrides
from lore_vault.storage import settings_store

log = logging.getLogger("lore_vault.api.runtime")

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class RuntimeConfig:
    def __init__(self, base: Config, connection: ConnectionFactory) -> None:
        self.base = base
        self._connection = connection
        self._lock = threading.Lock()
        self._cached: Config | None = None
        # 每次失效加一：讀 DB 期間若被失效，讀到的可能是舊值，不寫進快取
        self._generation = 0

    def current(self) -> Config:
        with self._lock:
            if self._cached is not None:
                return self._cached
            generation = self._generation
        try:
            with self._connection() as conn:
                values, _, invalid = settings_store.effective_overrides(conn)
        except sqlite3.Error as exc:
            log.warning("讀取執行期設定失敗，暫用設定檔的值：%s", exc)
            return self.base
        for row, reason in invalid:
            log.warning("略過不合法的設定覆寫 %s：%s", row.key, reason)
        try:
            config = apply_overrides(self.base, values)
        except (ConfigError, InvalidSetting) as exc:
            log.warning("設定覆寫合起來不合法，暫用設定檔的值：%s", exc)
            config = self.base
        with self._lock:
            if generation == self._generation:
                self._cached = config
        return config

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._generation += 1
