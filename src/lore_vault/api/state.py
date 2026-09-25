"""app 執行期狀態：連線工廠、查詢 embedder、背景 worker。"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lore_vault.enrich.clients import EnrichTimeout, OllamaEmbedder
from lore_vault.enrich.command import build_worker
from lore_vault.enrich.worker import EnrichWorker
from lore_vault.recall.embedder import Embedder
from lore_vault.storage.db import connect
from lore_vault.storage.snapshot import SnapshotCache

from .background import BackgroundEnricher
from .settings import ApiSettings


class QueryEmbedder:
    """請求路徑用的 Ollama embedder：短逾時（`embedding.query_timeout`），
    逾時轉成 `TimeoutError`，讓 recall／查重標成 `embedder_timeout` 降級。"""

    def __init__(self, inner: OllamaEmbedder) -> None:
        self._inner = inner

    def __repr__(self) -> str:
        return f"QueryEmbedder({self._inner!r}, timeout={self._inner.timeout})"

    def embed(self, text: str) -> Sequence[float]:
        try:
            return self._inner.embed(text)
        except EnrichTimeout as exc:
            raise TimeoutError(str(exc)) from None


def default_query_embedder(settings: ApiSettings) -> Embedder:
    cfg = settings.config.embedding
    return QueryEmbedder(
        OllamaEmbedder(dataclasses.replace(cfg, timeout=cfg.query_timeout))
    )


class AppState:
    def __init__(self, settings: ApiSettings) -> None:
        self.settings = settings
        self.dim = settings.config.embedding.dim
        self.query_embedder: Embedder = (
            settings.query_embedder
            if settings.query_embedder is not None
            else default_query_embedder(settings)
        )
        self.enricher: BackgroundEnricher | None = None
        if settings.run_worker:
            self.enricher = BackgroundEnricher(
                settings.db_path,
                settings.worker_factory or self._default_worker,
                poll_interval=settings.config.worker.poll_interval,
                join_timeout=settings.worker_join_timeout,
            )
        self._snapshot_cache: SnapshotCache | None = None
        self._snapshot_tmp: Path | None = None
        self._snapshot_lock = threading.Lock()

    def _default_worker(
        self, conn: sqlite3.Connection, should_stop: Callable[[], bool]
    ) -> EnrichWorker:
        return build_worker(
            conn,
            self.settings.config,
            self.settings.openai_key,
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
