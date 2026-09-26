"""啟動後的 embedding 暖機：背景執行緒呼叫一次 embed，讓 Ollama 先載入模型。

Ollama 冷啟動載入 bge-m3 約 2 秒，逼近請求路徑的 `embedding.query_timeout`；
服務剛起來時 recall／write 查重會因此短暫降級。暖機用完整的 `embedding.timeout`
（不是 query_timeout），不阻擋啟動；失敗只記 log，結果由 `/v1/status` 回報。
暖機失敗不影響 `ok`：它只代表第一批請求可能降級，不是資料或服務故障。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from lore_vault.recall.embedder import Embedder
from lore_vault.storage.timeutil import utc_now

logger = logging.getLogger("lore_vault.api.warmup")

WARMUP_TEXT = "lore vault warmup"

# 狀態中錯誤訊息最多保留幾個字
_MAX_ERROR = 300


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 1] + "…"


class EmbeddingWarmup:
    def __init__(self, embedder: Embedder | None, *, enabled: bool = True) -> None:
        self._embedder = embedder
        self._enabled = enabled and embedder is not None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "pending" if self._enabled else "disabled"
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._elapsed_ms: int | None = None
        self._error: str | None = None
        if not self._enabled:
            self._done.set()

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="lore-vault-embed-warmup", daemon=True
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        """等暖機結束（測試用）；回傳是否已結束。"""
        return self._done.wait(timeout)

    def _run(self) -> None:
        assert self._embedder is not None
        with self._lock:
            self._status = "running"
            self._started_at = utc_now()
        began = time.monotonic()
        try:
            vector = self._embedder.embed(WARMUP_TEXT)
            if not vector:
                raise ValueError("embedder 回傳空向量")
        except Exception as exc:  # noqa: BLE001 - 暖機失敗只記 log，不影響服務
            message = _describe(exc)
            logger.warning("embedding 暖機失敗：%s", message)
            with self._lock:
                self._status = "failed"
                self._error = message
        else:
            with self._lock:
                self._status = "ok"
        finally:
            with self._lock:
                self._elapsed_ms = int((time.monotonic() - began) * 1000)
                self._finished_at = utc_now()
            self._done.set()
        if self._status == "ok":
            logger.info("embedding 暖機完成（%d ms）", self._elapsed_ms)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed_ms": self._elapsed_ms,
                "error": self._error,
            }
