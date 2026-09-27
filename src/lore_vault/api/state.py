"""app 執行期狀態：連線工廠、查詢 embedder、背景 worker。"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lore_vault.ask.client import Answerer, OpenAIAnswerer
from lore_vault.enrich.clients import (
    EnrichTimeout,
    OllamaEmbedder,
    ProviderUnavailable,
    ollama_model_loaded,
    urllib_transport,
)
from lore_vault.enrich.command import build_worker
from lore_vault.enrich.worker import EnrichWorker
from lore_vault.recall.embedder import Embedder
from lore_vault.storage import document_index
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect
from lore_vault.storage.snapshot import SnapshotCache

from .background import BackgroundEnricher
from .settings import ApiSettings
from .warmup import EmbeddingWarmup

# `/api/ps` 探測的逾時（秒）：探測本身不能拖慢請求
PROBE_TIMEOUT = 1.0
# 最近一次成功呼叫後的這段時間內視為模型仍在記憶體，不再探測（遠小於 keep_alive）
HOT_WINDOW = 60.0


class QueryEmbedder:
    """請求路徑用的 Ollama embedder，逾時轉成 `TimeoutError`，讓 recall／查重標成
    `embedder_timeout` 降級。

    逾時依模型是否已載入決定：已載入用短逾時（`embedding.query_timeout`）；
    Ollama `/api/ps` 回報模型未載入（冷啟動）時改用 `embedding.cold_query_timeout`，
    閒置被卸載後的第一次查詢才不會必定降級。最近一次成功呼叫在 `HOT_WINDOW` 內不探測；
    探測失敗（None）維持短逾時——Ollama 狀況不明時不把請求拖長。
    """

    def __init__(
        self,
        inner: OllamaEmbedder,
        *,
        cold: OllamaEmbedder | None = None,
        probe: Callable[[], bool | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._cold = cold
        self._probe = probe
        self._clock = clock
        self._last_ok: float | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        cold = self._cold.timeout if self._cold is not None else None
        return (
            f"QueryEmbedder({self._inner!r}, timeout={self._inner.timeout}, "
            f"cold_timeout={cold})"
        )

    def model_loaded(self) -> bool | None:
        """模型目前是否載入（`/api/ps`）；無法判斷回 None。"""
        return self._probe() if self._probe is not None else None

    def _pick(self) -> OllamaEmbedder:
        if self._cold is None or self._probe is None:
            return self._inner
        with self._lock:
            last = self._last_ok
        if last is not None and self._clock() - last < HOT_WINDOW:
            return self._inner
        return self._cold if self._probe() is False else self._inner

    def embed(self, text: str) -> Sequence[float]:
        embedder = self._pick()
        try:
            vector = embedder.embed(text)
        except EnrichTimeout as exc:
            raise TimeoutError(str(exc)) from None
        with self._lock:
            self._last_ok = self._clock()
        return vector


def _transport(settings: ApiSettings):
    return settings.embed_transport or urllib_transport


def default_query_embedder(settings: ApiSettings) -> Embedder:
    cfg = settings.config.embedding
    transport = _transport(settings)
    return QueryEmbedder(
        OllamaEmbedder(
            dataclasses.replace(cfg, timeout=cfg.query_timeout), transport=transport
        ),
        cold=OllamaEmbedder(
            dataclasses.replace(cfg, timeout=cfg.cold_query_timeout),
            transport=transport,
        ),
        probe=lambda: ollama_model_loaded(
            cfg.base_url, cfg.model, transport=transport, timeout=PROBE_TIMEOUT
        ),
    )


def _document_progress(conn: sqlite3.Connection, stats: dict[str, Any]) -> str | None:
    from lore_vault.documents.worker import has_activity, progress_line

    if not has_activity(stats):
        return None
    try:
        pending = document_index.pending_counts(conn)
    except Exception:  # noqa: BLE001 - 進度 log 失敗不影響 worker
        pending = (-1, -1)
    return progress_line(stats, pending)


def default_answerer(settings: ApiSettings) -> Answerer | None:
    """依 `[ask]` 設定建立問答用戶端；沒有 OpenAI key 時回 None（ask 回
    `ask_not_configured`，doctor `ask.provider` 為 warn）。"""
    try:
        return OpenAIAnswerer(
            settings.config.ask,
            settings.openai_key,
            transport=settings.llm_transport or urllib_transport,
        )
    except ProviderUnavailable:
        return None


def default_warmup_embedder(settings: ApiSettings) -> Embedder:
    """暖機用完整的 `embedding.timeout`：冷啟動可能超過 query_timeout，
    用短逾時等於沒暖。"""
    return OllamaEmbedder(settings.config.embedding, transport=_transport(settings))


class AppState:
    def __init__(self, settings: ApiSettings) -> None:
        self.settings = settings
        self.dim = settings.config.embedding.dim
        self.query_embedder: Embedder = (
            settings.query_embedder
            if settings.query_embedder is not None
            else default_query_embedder(settings)
        )
        self.answerer: Answerer | None = (
            settings.answerer
            if settings.answerer is not None
            else default_answerer(settings)
        )
        self.warmup = EmbeddingWarmup(
            default_warmup_embedder(settings) if settings.run_warmup else None,
            enabled=settings.run_warmup,
        )
        self.enricher: BackgroundEnricher | None = None
        if settings.run_worker:
            self.enricher = BackgroundEnricher(
                settings.db_path,
                settings.worker_factory or self._default_worker,
                poll_interval=settings.config.worker.poll_interval,
                join_timeout=settings.worker_join_timeout,
            )
        self.documents_worker: BackgroundEnricher | None = None
        if settings.run_document_worker:
            self.documents_worker = BackgroundEnricher(
                settings.db_path,
                settings.document_worker_factory or self._default_document_worker,
                poll_interval=settings.config.worker.poll_interval,
                join_timeout=settings.worker_join_timeout,
                name="lore-vault-documents",
                label="文件",
                progress=_document_progress,
            )
        self._snapshot_cache: SnapshotCache | None = None
        self._snapshot_tmp: Path | None = None
        self._snapshot_lock = threading.Lock()

    def embedding_model_loaded(self) -> bool | None:
        probe = getattr(self.query_embedder, "model_loaded", None)
        return probe() if callable(probe) else None

    def _default_worker(
        self, conn: sqlite3.Connection, should_stop: Callable[[], bool]
    ) -> EnrichWorker:
        return build_worker(
            conn,
            self.settings.config,
            self.settings.openai_key,
            should_stop=should_stop,
        )

    def _default_document_worker(
        self, conn: sqlite3.Connection, should_stop: Callable[[], bool]
    ) -> Any:
        from lore_vault.documents.worker import build_document_worker

        return build_document_worker(
            conn,
            self.settings.config,
            transport=_transport(self.settings),
            should_stop=should_stop,
        )

    def migrate(self) -> None:
        """啟動時遷移（每個請求的連線不再遷移）。"""
        connect(self.settings.db_path).close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """每請求一條連線，在同一執行緒內開、用、關（sqlite3 連線不跨執行緒）。"""
        conn = connect(self.settings.db_path, run_migrations=False)
        try:
            yield conn
        finally:
            conn.close()

    def snapshot_cache(self) -> SnapshotCache:
        with self._snapshot_lock:
            if self._snapshot_cache is None:
                cache_dir = self.settings.snapshot_cache_dir
                if cache_dir is None:
                    cache_dir = self._snapshot_tmp = Path(
                        tempfile.mkdtemp(prefix="lore-snapshot-cache-")
                    )
                self._snapshot_cache = SnapshotCache(self.settings.db_path, cache_dir)
            return self._snapshot_cache

    def close(self) -> None:
        """關閉時清掉自建的快照快取目錄（設定指定的目錄不動）。"""
        if self._snapshot_tmp is not None:
            shutil.rmtree(self._snapshot_tmp, ignore_errors=True)
            self._snapshot_tmp = None
            self._snapshot_cache = None

    def wake_worker(self) -> None:
        if self.enricher is not None:
            self.enricher.wake()

    def worker_status(self) -> dict[str, Any]:
        if self.enricher is None:
            return {"enabled": False, "running": False}
        return self.enricher.status()

    def blob_store(self) -> BlobStore:
        blob_dir = self.settings.config.documents.blob_dir
        if not blob_dir:
            raise RuntimeError("未設定 documents.blob_dir")
        return BlobStore(blob_dir)

    def wake_documents(self) -> None:
        if self.documents_worker is not None:
            self.documents_worker.wake()

    def documents_worker_status(self) -> dict[str, Any]:
        if self.documents_worker is None:
            return {"enabled": False, "running": False}
        return self.documents_worker.status()
