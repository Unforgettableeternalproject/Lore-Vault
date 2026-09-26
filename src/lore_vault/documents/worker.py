"""文件背景 worker（T-63／T-64）：抽取 → 切段 → 寫 chunk 與 FTS → chunk 向量補算。

比照 `enrich.worker.EnrichWorker`：每輪 `run_once()`、候選在 autocommit 下讀、
模型呼叫期間不持有寫鎖、有上限重試；由 `api.background.BackgroundEnricher`
在服務程序內的背景執行緒執行（A9：單一寫入程序）。

狀態機（設計 4.4）：pending →（claim）extracting → ready／failed。
- `ExtractionError`（格式不支援、加密、損毀、空結果、過大、編碼不支援）是決定性的：
  直接 failed + error_code，不重試（要重跑就重新上傳）。
- 其他例外（含 blob 遺失／損毀）可能是暫時性的：記一次嘗試、退避後重試；
  達 `worker.max_attempts` 標 failed（error_code=corrupt，detail 記原因）。
- 程序中斷遺留的 extracting：worker 第一輪收回 pending。

向量：可索引文件（ready、未被取代）中缺向量的 chunk，逐一呼叫 embedder（沿用
`embedding` 設定，含 keep_alive 與每分鐘上限）。失敗以文件為單位記嘗試，達上限
不再自動補（doctor `documents.failed` 列出；lexical 仍可命中）。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lore_vault.config import Config
from lore_vault.enrich.clients import (
    EnrichError,
    InvalidOutput,
    OllamaEmbedder,
    ProviderUnavailable,
    RateLimited,
    Transport,
    urllib_transport,
)
from lore_vault.enrich.worker import STOPPED_SHUTDOWN, RateLimiter
from lore_vault.storage import document_index as index
from lore_vault.storage.blobs import BlobError, BlobStore
from lore_vault.storage.chunk_vectors import set_chunk_embedding
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import DimensionMismatch
from lore_vault.storage.timeutil import format_utc

from .chunking import chunk_segments
from .extract import CORRUPT, ExtractionError, Limits, extract


@dataclass
class ExtractStats:
    # ready
    done: int = 0
    # 決定性失敗（直接 failed）
    failed: int = 0
    # 非預期失敗，退避後重試
    retry: int = 0
    # 非預期失敗達上限、標 failed
    gave_up: int = 0
    # 處理期間文件被刪或被別人處理
    stale: int = 0
    stopped: str | None = None


@dataclass
class EmbedStats:
    done: int = 0
    retry: int = 0
    gave_up: int = 0
    stale: int = 0
    stopped: str | None = None


@dataclass
class DocumentRunStats:
    extract: ExtractStats = field(default_factory=ExtractStats)
    embedding: EmbedStats = field(default_factory=EmbedStats)
    # 本輪開始時從中斷遺留的 extracting 收回 pending 的數量
    recovered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "extract": dict(vars(self.extract)),
            "embedding": dict(vars(self.embedding)),
            "recovered": self.recovered,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class DocumentWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        *,
        blobs: BlobStore,
        embedder: Any | None,
        embed_limiter: RateLimiter | None = None,
        now: Callable[[], datetime] = _utc_now,
        should_stop: Callable[[], bool] | None = None,
        unavailable: str | None = None,
    ) -> None:
        """`embedder` 為 None 時本輪略過向量補算（`unavailable` 為原因）。"""
        self.conn = conn
        self.config = config
        self.blobs = blobs
        self.embedder = embedder
        self.embed_limiter = embed_limiter or RateLimiter(0)
        self.now = now
        self.should_stop = should_stop or (lambda: False)
        self.unavailable = unavailable or "未設定"
        self.limits = Limits.from_config(config.documents)
        self._recovered = False

    def run_once(self, *, limit: int | None = None) -> DocumentRunStats:
        stats = DocumentRunStats()
        if not self._recovered:
            stats.recovered = index.recover_interrupted(self.conn)
            self._recovered = True
        batch = limit if limit is not None else self.config.worker.batch_size
        self._extract_round(stats.extract, batch)
        if stats.extract.stopped is None:
            self._embed_round(stats.embedding, batch)
        else:
            stats.embedding.stopped = stats.extract.stopped
        return stats

    # ── 抽取 ──

    def _extract_round(self, stats: ExtractStats, batch: int) -> None:
        todo = index.extract_candidates(
            self.conn, now=format_utc(self.now()), limit=batch
        )
        for item in todo:
            if self.should_stop():
                stats.stopped = STOPPED_SHUTDOWN
                return
            if not index.claim(self.conn, item.id):
                stats.stale += 1
                continue
            self._extract_one(item, stats)

    def _extract_one(self, item: index.ExtractCandidate, stats: ExtractStats) -> None:
        docs = self.config.documents
        try:
            data = self.blobs.read(item.sha256)
            result = extract(data, item.filename, item.mime, limits=self.limits)
            chunks = chunk_segments(
                result.segments,
                max_tokens=docs.chunk_max_tokens,
                overlap_tokens=docs.chunk_overlap_tokens,
            )
        except ExtractionError as exc:
            if index.finish_failed(self.conn, item.id, exc.code, exc.detail):
                stats.failed += 1
            else:
                stats.stale += 1
            return
        except Exception as exc:  # noqa: BLE001 - 非預期失敗走有上限重試，不讓迴圈掛掉
            detail = _describe(exc)
            if isinstance(exc, BlobError):
                detail = f"原始檔 blob 無法讀取（{detail}）"
            status = index.record_extract_failure(
                self.conn,
                item.id,
                detail,
                fail_code=CORRUPT,
                now=self.now(),
                max_attempts=self.config.worker.max_attempts,
                backoff_seconds=self.config.worker.retry_backoff,
            )
            if status is None:
                stats.stale += 1
            elif status == "failed":
                stats.gave_up += 1
            else:
                stats.retry += 1
            return
        if not chunks:
            # 抽取器保證 segments 非空白；這裡只是防守
            if index.finish_failed(
                self.conn, item.id, "empty_extraction", "切段後沒有任何 chunk"
            ):
                stats.failed += 1
            return
        if index.finish_ready(self.conn, item.id, chunks, encoding=result.encoding):
            stats.done += 1
        else:
            stats.stale += 1

    # ── 向量 ──

    def _embed_round(self, stats: EmbedStats, batch: int) -> None:
        if self.embedder is None:
            stats.stopped = self.unavailable
            return
        todo = index.embedding_candidates(
            self.conn, now=format_utc(self.now()), limit=batch
        )
        skipped: set[str] = set()
        for item in todo:
            if item.document_id in skipped:
                continue
            if self.should_stop():
                stats.stopped = STOPPED_SHUTDOWN
                return
            self.embed_limiter.acquire()
            try:
                written = self._embed_one(item)
            except ProviderUnavailable as exc:
                stats.stopped = str(exc)
                return
            except RateLimited as exc:
                self._fail(item, exc, stats)
                stats.stopped = str(exc)
                return
            except EnrichError as exc:
                self._fail(item, exc, stats)
                skipped.add(item.document_id)
                continue
            if written:
                stats.done += 1
            else:
                stats.stale += 1

    def _embed_one(self, item: index.EmbedCandidate) -> bool:
        assert self.embedder is not None
        vector = self.embedder.embed(item.text)
        dim = self.config.embedding.dim
        with transaction(self.conn):
            if not index.chunk_is_indexable(self.conn, item.seq):
                return False
            try:
                set_chunk_embedding(
                    self.conn,
                    item.seq,
                    vector,
                    dim=dim,
                    model=getattr(self.embedder, "model", None),
                )
            except DimensionMismatch as exc:
                raise InvalidOutput(f"embedding 不合法：{exc}") from None
            index.clear_embedding_attempts_if_done(self.conn, item.document_id)
        return True

    def _fail(
        self, item: index.EmbedCandidate, exc: Exception, stats: EmbedStats
    ) -> None:
        status = index.record_embedding_failure(
            self.conn,
            item.document_id,
            _describe(exc),
            now=self.now(),
            max_attempts=self.config.worker.max_attempts,
            backoff_seconds=self.config.worker.retry_backoff,
        )
        if status == "failed":
            stats.gave_up += 1
        else:
            stats.retry += 1


def build_document_worker(
    conn: sqlite3.Connection,
    config: Config,
    *,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] | None = None,
) -> DocumentWorker:
    """依設定建立文件 worker（`documents.blob_dir` 必須已設定）。

    embedder 與 note 補算同一套設定（`embedding.*`：逾時、keep_alive、每分鐘上限）。
    """
    if not config.documents.blob_dir:
        raise ValueError("未設定 documents.blob_dir，無法處理文件")
    return DocumentWorker(
        conn,
        config,
        blobs=BlobStore(config.documents.blob_dir),
        embedder=OllamaEmbedder(config.embedding, transport=transport),
        embed_limiter=RateLimiter(config.embedding.rate_per_minute, sleep=sleep),
        should_stop=should_stop,
    )


def progress_line(stats: dict[str, Any], pending: tuple[int, int]) -> str:
    """文件 worker 每輪有動作時的一行進度（比照補算的 `_progress_line`）。"""
    ex = stats.get("extract", {})
    em = stats.get("embedding", {})
    return (
        f"文件本輪：抽取 完成 {ex.get('done', 0)}／失敗 {ex.get('failed', 0)}"
        f"／重試 {ex.get('retry', 0)}／放棄 {ex.get('gave_up', 0)}；"
        f"向量 完成 {em.get('done', 0)}／重試 {em.get('retry', 0)}"
        f"／放棄 {em.get('gave_up', 0)}。"
        f"剩餘 待抽取 {pending[0]}、缺向量 chunk {pending[1]}"
    )


def has_activity(stats: dict[str, Any]) -> bool:
    if stats.get("recovered"):
        return True
    for kind in ("extract", "embedding"):
        values = stats.get(kind, {})
        if any(
            values.get(name)
            for name in ("done", "failed", "retry", "gave_up", "stale", "stopped")
        ):
            return True
    return False
