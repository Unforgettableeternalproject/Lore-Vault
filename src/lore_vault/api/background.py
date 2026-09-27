"""同一程序內的背景補算（A9：所有寫入都在服務這一個程序內）。

- 執行緒自己開一條 SQLite 連線（連線不跨執行緒共用）；WAL + busy_timeout
  讓它與請求端的連線並存
- `stop()` 設旗標並喚醒等待：兩輪之間立刻結束；進行中的一輪在下一則 note 前
  中止（`EnrichWorker.should_stop`）。已送出的 HTTP 呼叫無法中斷，最多等
  `join_timeout` 秒；執行緒是 daemon，不會卡住程序結束
- SIGTERM 由 uvicorn 接手 → lifespan 結束 → `stop()`；這裡不另裝訊號處理
- 單輪例外記 log 後繼續下一輪，執行緒不會靜默死掉；狀態由 `status()` 回報
- 同一個類別也跑文件 worker（抽取／切段／chunk 向量）：以 `name`、`label`、
  `progress`（有動作時的一行 log）區分，預設是 note 補算
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from os import PathLike
from typing import Any

from lore_vault.storage.db import connect
from lore_vault.storage.timeutil import utc_now

from .settings import WorkerFactory

logger = logging.getLogger("lore_vault.api.worker")

# log 與狀態中錯誤訊息最多保留幾個字
_MAX_ERROR = 300


def _pending(conn: Any) -> tuple[int, int]:
    """目前待補的摘要與向量筆數（供每輪 log 用）。"""
    summary = conn.execute(
        "SELECT count(*) FROM notes WHERE summary IS NULL"
    ).fetchone()
    embedding = conn.execute(
        "SELECT count(*) FROM notes n LEFT JOIN note_embeddings e"
        " ON e.note_seq = n.seq WHERE e.note_seq IS NULL"
    ).fetchone()
    return int(summary[0]), int(embedding[0])


def _progress_line(stats: dict[str, dict[str, Any]], pending: tuple[int, int]) -> str:
    parts = []
    for kind, label in (("summary", "摘要"), ("embedding", "向量")):
        k = stats.get(kind, {})
        parts.append(
            f"{label} 完成 {k.get('done', 0)}／重試 {k.get('retry', 0)}"
            f"／放棄 {k.get('gave_up', 0)}"
        )
    return (
        "補算本輪：" + "；".join(parts) + f"。剩餘 摘要 {pending[0]}、向量 {pending[1]}"
    )


def _has_activity(stats: dict[str, dict[str, Any]]) -> bool:
    return any(
        k.get(field)
        for k in stats.values()
        for field in ("done", "retry", "gave_up", "stale", "stopped")
    )


def _note_progress(conn: Any, stats: dict[str, Any]) -> str | None:
    """note 補算：有動作的輪次回一行進度，否則 None。"""
    if not _has_activity(stats):
        return None
    try:
        pending = _pending(conn)
    except Exception:  # noqa: BLE001 - 進度 log 失敗不影響補算
        pending = (-1, -1)
    return _progress_line(stats, pending)


# (背景執行緒自己的連線, 本輪 stats dict) -> 一行進度 log 或 None（沒動作不印）
ProgressFormatter = Callable[[Any, dict[str, Any]], str | None]


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
        name: str = "lore-vault-enrich",
        label: str = "補算",
        progress: ProgressFormatter = _note_progress,
    ) -> None:
        self._name = name
        self._label = label
        self._progress = progress
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
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
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
            logger.warning(
                "%s worker 未在時限內結束（可能卡在進行中的模型呼叫或抽取）",
                self._label,
            )
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
                    logger.warning("%s worker 本輪失敗：%s", self._label, message)
                    with self._lock:
                        self._last_error = message
                        self._last_run = utc_now()
                        self._runs += 1
                else:
                    stats_dict = stats.to_dict()
                    # 有動作的輪次才印一行進度，避免每 poll_interval 洗版
                    try:
                        line = self._progress(conn, stats_dict)
                    except Exception:  # noqa: BLE001 - 進度 log 失敗不影響補算
                        line = None
                    if line:
                        logger.info(line)
                    with self._lock:
                        self._last_stats = stats_dict
                        self._last_error = None
                        self._last_run = utc_now()
                        self._runs += 1
                self._wake.wait(self._poll_interval)
        except Exception as exc:  # noqa: BLE001 - 啟動失敗（開不了 DB 等）要能在 status 看到
            message = _describe(exc)
            logger.error("%s worker 無法執行：%s", self._label, message)
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
