"""同一程序內的背景補算（A9：所有寫入都在服務這一個程序內）。

- 執行緒自己開一條 SQLite 連線（連線不跨執行緒共用）；WAL + busy_timeout
  讓它與請求端的連線並存
- `stop()` 設旗標並喚醒等待：兩輪之間立刻結束；進行中的一輪在下一則 note 前
  中止（`EnrichWorker.should_stop`）。已送出的 HTTP 呼叫無法中斷，最多等
  `join_timeout` 秒；執行緒是 daemon，不會卡住程序結束
- SIGTERM 由 uvicorn 接手 → lifespan 結束 → `stop()`；這裡不另裝訊號處理
- 單輪例外記 log 後繼續下一輪，執行緒不會靜默死掉；狀態由 `status()` 回報
"""

from __future__ import annotations

import logging
import threading
from os import PathLike
from typing import Any

from lore_vault.storage.db import connect
from lore_vault.storage.timeutil import utc_now

from .settings import WorkerFactory

logger = logging.getLogger("lore_vault.api.worker")

# log 與狀態中錯誤訊息最多保留幾個字
_MAX_ERROR = 300


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 1] + "…"


class BackgroundEnricher:
    def __init__(
        self,
        db_path: str | PathLike[str],
        factory: WorkerFactory,
        *,
        poll_interval: float,
        join_timeout: float = 5.0,
    ) -> None:
        self._db_path = db_path
        self._factory = factory
        self._poll_interval = poll_interval
        self._join_timeout = join_timeout
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._runs = 0
        self._last_run: str | None = None
        self._last_stats: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._fatal: str | None = None

    # ── 生命週期 ──

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker 已啟動")
        self._thread = threading.Thread(
            target=self._run, name="lore-vault-enrich", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        """有新資料（write／update）時提早開始下一輪。"""
        self._wake.set()

    def stop(self, timeout: float | None = None) -> bool:
        """要求停止並等待執行緒結束；回傳是否已結束。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(self._join_timeout if timeout is None else timeout)
        stopped = not thread.is_alive()
        if not stopped:
            logger.warning("補算 worker 未在時限內結束（可能卡在進行中的模型呼叫）")
        return stopped

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── 執行緒本體 ──

    def _run(self) -> None:
        conn = None
        try:
            conn = connect(self._db_path, run_migrations=False)
            worker = self._factory(conn, self._stop.is_set)
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    stats = worker.run_once()
                except Exception as exc:  # noqa: BLE001 - 單輪失敗不能讓執行緒死掉
                    message = _describe(exc)
                    logger.warning("補算 worker 本輪失敗：%s", message)
                    with self._lock:
                        self._last_error = message
                        self._last_run = utc_now()
                        self._runs += 1
                else:
                    with self._lock:
                        self._last_stats = stats.to_dict()
                        self._last_error = None
                        self._last_run = utc_now()
                        self._runs += 1
                self._wake.wait(self._poll_interval)
        except Exception as exc:  # noqa: BLE001 - 啟動失敗（開不了 DB 等）要能在 status 看到
            message = _describe(exc)
            logger.error("補算 worker 無法執行：%s", message)
            with self._lock:
                self._fatal = message
        finally:
            if conn is not None:
                conn.close()

    # ── 狀態 ──

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "running": self.running,
                "stopping": self._stop.is_set(),
                "runs": self._runs,
                "last_run": self._last_run,
                "last_stats": self._last_stats,
                "last_error": self._last_error,
                "fatal_error": self._fatal,
                "poll_interval": self._poll_interval,
            }
