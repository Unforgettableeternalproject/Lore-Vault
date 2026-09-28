"""啟動後的 embedding 暖機：背景執行緒呼叫 embed，讓 Ollama 先載入模型。

Ollama 冷啟動載入 bge-m3 約 2 秒，逼近請求路徑的 `embedding.query_timeout`；
服務剛起來時 recall／write 查重會因此短暫降級。暖機用完整的 `embedding.timeout`
（不是 query_timeout），不阻擋啟動；結果由 `/v1/status` 回報。
暖機失敗不影響 `ok`：它只代表第一批請求可能降級，不是資料或服務故障。

連線類失敗（`ProviderUnavailable`、連線拒絕、逾時）多半是 Ollama 比服務晚起來，
改在背景以退避重試（預設 5 秒起倍增、單次上限 60 秒、總時長上限 10 分鐘）：
重試期間狀態為 `retrying`（附 `attempts` 與最近一次 `error`），成功轉 `ok`，
用完總時長才 `failed`。其他錯誤（例如空向量）直接 `failed`，不重試。
停止服務時 `stop()` 會中斷等待中的重試。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from lore_vault.enrich.clients import EnrichTimeout, ProviderUnavailable
from lore_vault.recall.embedder import Embedder
from lore_vault.storage.timeutil import utc_now

logger = logging.getLogger("lore_vault.api.warmup")

WARMUP_TEXT = "lore vault warmup"

# 重試退避預設值（秒）
RETRY_INITIAL_DELAY = 5.0
RETRY_MAX_DELAY = 60.0
RETRY_MAX_TOTAL = 600.0

# 視為「Ollama 還沒就緒」、值得重試的錯誤
_RETRYABLE: tuple[type[BaseException], ...] = (
    ProviderUnavailable,
    EnrichTimeout,
    ConnectionError,
    TimeoutError,
)

# 狀態中錯誤訊息最多保留幾個字
_MAX_ERROR = 300


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_ERROR else text[: _MAX_ERROR - 1] + "…"


class EmbeddingWarmup:
    def __init__(
        self,
        embedder: Embedder | None,
        *,
        enabled: bool = True,
        initial_delay: float = RETRY_INITIAL_DELAY,
        max_delay: float = RETRY_MAX_DELAY,
        max_total: float = RETRY_MAX_TOTAL,
        wait: Callable[[float], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._embedder = embedder
        self._enabled = enabled and embedder is not None
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._max_total = max_total
        self._clock = clock
        self._stop = threading.Event()
        # 等待下一次重試；回傳 True 代表收到停止訊號（測試可注入，不真的睡）
        self._wait_retry = wait or self._stop.wait
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "pending" if self._enabled else "disabled"
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._elapsed_ms: int | None = None
        self._error: str | None = None
        self._attempts = 0
        if not self._enabled:
            self._done.set()

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="lore-vault-embed-warmup", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        """中斷重試（服務關閉時）。進行中的 embed 不強制中斷；
        daemon 執行緒不擋關閉。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """等暖機結束（測試用）；回傳是否已結束。"""
        return self._done.wait(timeout)

    def _attempt(self) -> None:
        assert self._embedder is not None
        vector = self._embedder.embed(WARMUP_TEXT)
        if not vector:
            raise ValueError("embedder 回傳空向量")

    def _run(self) -> None:
        with self._lock:
            self._status = "running"
            self._started_at = utc_now()
        began = time.monotonic()
        retry_began = self._clock()
        delay = self._initial_delay
        ok = False
        try:
            while True:
                with self._lock:
                    self._attempts += 1
                    attempts = self._attempts
                try:
                    self._attempt()
                except Exception as exc:  # noqa: BLE001 - 暖機失敗只記 log，不影響服務
                    message = _describe(exc)
                    if not isinstance(exc, _RETRYABLE):
                        logger.warning("embedding 暖機失敗：%s", message)
                        self._set_failed(message)
                        return
                    spent = self._clock() - retry_began
                    if spent + delay > self._max_total:
                        logger.warning(
                            "embedding 暖機失敗（已重試 %d 次，放棄）：%s",
                            attempts,
                            message,
                        )
                        self._set_failed(message)
                        return
                    logger.warning(
                        "embedding 暖機第 %d 次失敗，%.0f 秒後重試：%s",
                        attempts,
                        delay,
                        message,
                    )
                    with self._lock:
                        self._status = "retrying"
                        self._error = message
                    if self._stop.is_set() or self._wait_retry(delay):
                        self._set_failed(f"服務停止，暖機中止（最近錯誤：{message}）")
                        return
                    delay = min(delay * 2, self._max_delay)
                else:
                    with self._lock:
                        self._status = "ok"
                        self._error = None
                    ok = True
                    return
        finally:
            with self._lock:
                self._elapsed_ms = int((time.monotonic() - began) * 1000)
                self._finished_at = utc_now()
            self._done.set()
            if ok:
                logger.info("embedding 暖機完成（%d ms）", self._elapsed_ms)

    def _set_failed(self, message: str) -> None:
        with self._lock:
            self._status = "failed"
            self._error = message

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed_ms": self._elapsed_ms,
                "error": self._error,
                "attempts": self._attempts,
            }
