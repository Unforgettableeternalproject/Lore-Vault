"""背景補算 worker（D4／A14）：找出缺 summary／缺 embedding 的 note，限速呼叫模型、
有上限重試，超過上限標記失敗。

- 讀候選在 autocommit 下做；HTTP 呼叫期間不持有寫鎖。
- 寫回時比對讀取當下的 `updated`：補算期間 note 被更新就丟棄結果（stale），
  新版本留給下一輪。
- 時間（UTC 牆鐘）、限速用的單調時鐘與 sleep 都可注入，測試不真的等。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lore_vault.config import WorkerConfig
from lore_vault.storage import enrichment as store
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import DimensionMismatch
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vectors import set_embedding

from .clients import (
    Embedder,
    EnrichError,
    InvalidOutput,
    ProviderUnavailable,
    RateLimited,
    Summarizer,
)


class RateLimiter:
    """每分鐘上限 → 兩次呼叫的最小間隔。`per_minute <= 0` 表示不限。"""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def acquire(self) -> None:
        if self.interval > 0 and self._last is not None:
            wait = self._last + self.interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()


@dataclass
class KindStats:
    done: int = 0
    # 失敗一次（仍會重試）
    retry: int = 0
    # 這一輪達到上限、標記失敗
    gave_up: int = 0
    # 補算期間 note 被更新或刪除，結果丟棄
    stale: int = 0
    # 本輪提早停止的原因（服務不可用、429、未設定）
    stopped: str | None = None


@dataclass
class RunStats:
    summary: KindStats = field(default_factory=KindStats)
    embedding: KindStats = field(default_factory=KindStats)

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {
            kind: dict(vars(getattr(self, kind))) for kind in ("summary", "embedding")
        }


def embedding_text(title: str, body: str) -> str:
    """embedding 的輸入：title + body（與 storage 在 title／body 變動時刪向量一致）。"""
    return f"{title}\n\n{body}" if body else title


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EnrichWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: WorkerConfig,
        *,
        embedder: Embedder | None,
        summarizer: Summarizer | None,
        embed_limiter: RateLimiter | None = None,
        summary_limiter: RateLimiter | None = None,
        now: Callable[[], datetime] = _utc_now,
        unavailable: dict[str, str] | None = None,
    ) -> None:
        """`embedder`／`summarizer` 為 None 時該種補算本輪略過；
        `unavailable` 可帶略過原因（例如缺 API key）。"""
        self.conn = conn
        self.config = config
        self.embedder = embedder
        self.summarizer = summarizer
        self.embed_limiter = embed_limiter or RateLimiter(0)
        self.summary_limiter = summary_limiter or RateLimiter(0)
        self.now = now
        self.unavailable = dict(unavailable or {})

    def run_once(self, *, limit: int | None = None) -> RunStats:
        stats = RunStats()
        batch = limit if limit is not None else self.config.batch_size
        self._run_kind("summary", stats.summary, batch)
        self._run_kind("embedding", stats.embedding, batch)
        return stats

    def _run_kind(self, kind: str, stats: KindStats, batch: int) -> None:
        client = self.summarizer if kind == "summary" else self.embedder
        if client is None:
            stats.stopped = self.unavailable.get(kind, "未設定")
            return
        limiter = self.summary_limiter if kind == "summary" else self.embed_limiter
        todo = store.candidates(
            self.conn, kind, now=format_utc(self.now()), limit=batch
        )
        for item in todo:
            limiter.acquire()
            try:
                if kind == "summary":
                    written = self._summarize(item)
                else:
                    written = self._embed(item)
            except ProviderUnavailable as exc:
                stats.stopped = str(exc)
                return
            except RateLimited as exc:
                self._fail(kind, item, exc, stats)
                stats.stopped = str(exc)
                return
            except EnrichError as exc:
                self._fail(kind, item, exc, stats)
                continue
            if written:
                stats.done += 1
            else:
                stats.stale += 1

    def _summarize(self, item: store.Candidate) -> bool:
        assert self.summarizer is not None
        summary = self.summarizer.summarize(item.title, item.body)
        return store.write_summary_if_current(
            self.conn, item.seq, item.updated, summary
        )

    def _embed(self, item: store.Candidate) -> bool:
        assert self.embedder is not None
        vector = self.embedder.embed(embedding_text(item.title, item.body))
        with transaction(self.conn):
            if not store.note_is_current(self.conn, item.seq, item.updated):
                return False
            try:
                set_embedding(
                    self.conn,
                    item.vault,
                    item.note_id,
                    vector,
                    dim=self.embedder.dim,
                    model=self.embedder.model,
                )
            except DimensionMismatch as exc:
                raise InvalidOutput(f"embedding 不合法：{exc}") from None
            store.clear(self.conn, item.seq, "embedding")
        return True

    def _fail(
        self, kind: str, item: store.Candidate, exc: Exception, stats: KindStats
    ) -> None:
        status = store.record_failure(
            self.conn,
            item.seq,
            kind,
            item.updated,
            f"{type(exc).__name__}: {exc}",
            now=self.now(),
            max_attempts=self.config.max_attempts,
            backoff_seconds=self.config.retry_backoff,
        )
        if status is None:
            stats.stale += 1
        elif status == "failed":
            stats.gave_up += 1
        else:
            stats.retry += 1
