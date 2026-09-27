"""背景補算 worker：成功、失敗類型、重試上限、race、限速、doctor 對帳、CLI。

全部以 fake HTTP／fake 用戶端執行，不打網路、不真的 sleep。
"""

from __future__ import annotations

import io
import json
import sqlite3

import pytest

from lore_vault.config import EmbeddingConfig, Secret, SummaryConfig, WorkerConfig
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.enrich import (
    EnrichTimeout,
    EnrichWorker,
    InvalidOutput,
    OllamaEmbedder,
    OpenAISummarizer,
    RateLimiter,
)
from lore_vault.enrich.command import main as enrich_main
from lore_vault.storage import enrichment as store
from lore_vault.storage import fts, vectors
from lore_vault.storage.migrate import MIGRATIONS, SCHEMA_VERSION, migrate
from lore_vault.storage.notes import get_note, list_notes, update_note_if

VAULT = "folder/enrich"
DIM = 4
KEY = "sk-test-FAKEKEY-0123456789"
VEC = [0.5, 0.1, 0.2, 0.3]


def make_worker(conn, clock, *, summary=None, embed=None, **worker_kw):
    """summary／embed：FakeTransport 或自訂用戶端物件；None 表示該種不跑。"""
    config = WorkerConfig(**{"max_attempts": 3, "retry_backoff": 60.0, **worker_kw})
    if summary is not None and not hasattr(summary, "summarize"):
        summary = OpenAISummarizer(SummaryConfig(), Secret(KEY), transport=summary)
    if embed is not None and not hasattr(embed, "embed"):
        embed = OllamaEmbedder(EmbeddingConfig(dim=DIM), transport=embed)
    return EnrichWorker(conn, config, embedder=embed, summarizer=summary, now=clock)


def enrichment_rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT note_seq, kind, attempts, status, last_error FROM note_enrichment"
        )
    ]


def doctor(conn, **settings):
    report = default_registry().run(
        DoctorContext(settings=settings, resources={"db": conn}),
        categories=["enrich"],
    )
    return {o.name: o.result for o in report.outcomes}


# ── 成功 ──


def test_success_writes_summary_and_embedding_without_bumping_version(
    conn, add_note, clock, http
):
    note = add_note("n-1", "儲存引擎", "決定採用 SQLite WAL。")
    worker = make_worker(
        conn,
        clock,
        summary=http.Transport(http.chat("採用 SQLite WAL，查詢 3ms。")),
        embed=http.Transport(http.embed(VEC)),
    )
    stats = worker.run_once()
    assert (stats.summary.done, stats.embedding.done) == (1, 1)

    stored = get_note(conn, VAULT, "n-1", space="dev")
    assert stored.summary == "採用 SQLite WAL，查詢 3ms。"
    # 衍生資料寫回不推進樂觀鎖版本
    assert stored.updated == note.updated
    # FTS 同步更新：摘要裡才有的詞搜得到
    assert [h.note_id for h in fts.search_notes(conn, VAULT, "查詢", space="dev")] == [
        "n-1"
    ]
    assert vectors.get_embedding(conn, VAULT, "n-1", space="dev") is not None
    assert enrichment_rows(conn) == []
    # 沒有候選了
    again = worker.run_once()
    assert (again.summary.done, again.embedding.done) == (0, 0)


def test_notes_with_summary_or_embedding_are_skipped(conn, add_note, clock, http):
    add_note("n-1", summary="已有摘要")
    vectors.set_embedding(conn, VAULT, "n-1", VEC, space="dev", dim=DIM)
    worker = make_worker(conn, clock, summary=http.Transport(), embed=http.Transport())
    stats = worker.run_once()  # 空 transport 被呼叫會 AssertionError
    assert (stats.summary.done, stats.embedding.done) == (0, 0)


# ── 失敗類型與重試上限 ──


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        ("empty", "空字串"),
        ("length", "length"),
        ("timeout", "EnrichTimeout"),
        ("503", "503"),
    ],
)
def test_failures_are_recorded_and_summary_stays_null(
    conn, add_note, clock, http, response, needle
):
    add_note("n-1")
    reply = {
        "empty": http.chat(""),
        "length": http.chat("截斷", finish="length"),
        "timeout": EnrichTimeout("請求逾時"),
        "503": http.error(503, "overloaded"),
    }[response]
    worker = make_worker(conn, clock, summary=http.Transport(reply))
    stats = worker.run_once()
    assert stats.summary.retry == 1 and stats.summary.done == 0
    assert get_note(conn, VAULT, "n-1", space="dev").summary is None
    [row] = enrichment_rows(conn)
    assert row["kind"] == "summary" and row["attempts"] == 1
    assert row["status"] == "pending"
    assert needle in row["last_error"]
    assert KEY not in row["last_error"]


def test_retry_limit_marks_failed_and_stops_retrying(conn, add_note, clock, http):
    add_note("n-1")
    fake = http.Transport(http.chat(""), http.chat(" "), http.chat(None))
    worker = make_worker(conn, clock, summary=fake, max_attempts=3)

    assert worker.run_once().summary.retry == 1
    # 退避期間不重試
    assert worker.run_once().summary.retry == 0
    assert len(fake.requests) == 1
    clock.advance(60)
    assert worker.run_once().summary.retry == 1
    clock.advance(120)
    stats = worker.run_once()
    assert stats.summary.gave_up == 1
    [row] = enrichment_rows(conn)
    assert (row["attempts"], row["status"]) == (3, "failed")

    # 超過上限後不再嘗試（空 transport 被呼叫會 AssertionError）
    clock.advance(10**6)
    assert worker.run_once().summary.done == 0
    assert len(fake.requests) == 3

    results = doctor(conn, now=clock())
    assert results["enrich.failed"].status is Status.FAIL
    assert results["enrich.failed"].counts["summary_failed"] == 1
    # 失敗的不算積壓
    assert results["enrich.backlog"].counts["summary_pending"] == 0

    # 人工 reset 後重新排入
    assert store.reset_failed(conn, "summary") == 1
    assert doctor(conn, now=clock())["enrich.failed"].status is Status.PASS


def test_rate_limited_stops_kind_for_this_run(conn, add_note, clock, http):
    add_note("n-1")
    add_note("n-2")
    fake = http.Transport(http.error(429, "slow", {"Retry-After": "30"}))
    worker = make_worker(conn, clock, summary=fake)
    stats = worker.run_once()
    assert stats.summary.retry == 1
    assert "429" in stats.summary.stopped
    assert len(fake.requests) == 1  # 第二則沒送
    assert len(enrichment_rows(conn)) == 1


def test_provider_unavailable_consumes_no_attempts(conn, add_note, clock, http):
    add_note("n-1")
    add_note("n-2")
    fake = http.Transport(http.error(401, f"bad key {KEY}"))
    stats = make_worker(conn, clock, summary=fake).run_once()
    assert "401" in stats.summary.stopped and KEY not in stats.summary.stopped
    assert enrichment_rows(conn) == []
    assert len(fake.requests) == 1


def test_missing_summarizer_still_runs_embedding(conn, add_note, clock, http):
    add_note("n-1")
    worker = EnrichWorker(
        conn,
        WorkerConfig(),
        embedder=OllamaEmbedder(
            EmbeddingConfig(dim=DIM), transport=http.Transport(http.embed(VEC))
        ),
        summarizer=None,
        now=clock,
        unavailable={"summary": "缺少 OPENAI_API_KEY"},
    )
    stats = worker.run_once()
    assert stats.summary.stopped == "缺少 OPENAI_API_KEY"
    assert stats.embedding.done == 1


def test_embedding_invalid_vector_is_failure(conn, add_note, clock, http):
    add_note("n-1")
    fake = http.Transport(http.embed([0.0, 0.0, 0.0, 0.0]))  # 零向量無法正規化
    stats = make_worker(conn, clock, embed=fake).run_once()
    assert stats.embedding.retry == 1
    assert vectors.get_embedding(conn, VAULT, "n-1", space="dev") is None


# ── race：補算期間 note 被更新 ──


class UpdatingSummarizer:
    """呼叫期間模擬 agent 更新 note（body 改了），再回傳舊內容的摘要。"""

    model = "fake"

    def __init__(self, conn, *, then_raise: bool = False) -> None:
        self.conn = conn
        self.then_raise = then_raise
        self.calls: list[str] = []

    def summarize(self, title: str, body: str) -> str:
        self.calls.append(body)
        if len(self.calls) == 1:
            current = get_note(self.conn, VAULT, "n-1", space="dev")
            update_note_if(
                self.conn,
                VAULT,
                "n-1",
                current.updated,
                {"body": "新版正文"},
                space="dev",
            )
            if self.then_raise:
                raise InvalidOutput("摘要為空字串")
        return f"摘要：{body}"


def test_race_summary_result_for_old_version_is_discarded(conn, add_note, clock):
    add_note("n-1", body="舊版正文")
    summarizer = UpdatingSummarizer(conn)
    worker = make_worker(conn, clock, summary=summarizer)

    stats = worker.run_once()
    assert stats.summary.stale == 1 and stats.summary.done == 0
    # 舊內容的摘要沒有蓋到新版本上
    assert get_note(conn, VAULT, "n-1", space="dev").summary is None

    # 下一輪以新版本補算
    stats = worker.run_once()
    assert stats.summary.done == 1
    assert get_note(conn, VAULT, "n-1", space="dev").summary == "摘要：新版正文"
    assert summarizer.calls == ["舊版正文", "新版正文"]


def test_race_failure_for_old_version_is_not_counted(conn, add_note, clock):
    add_note("n-1", body="舊版正文")
    worker = make_worker(conn, clock, summary=UpdatingSummarizer(conn, then_raise=True))
    stats = worker.run_once()
    assert stats.summary.stale == 1 and stats.summary.retry == 0
    assert enrichment_rows(conn) == []


class UpdatingEmbedder:
    model = "fake"
    dim = DIM

    def __init__(self, conn) -> None:
        self.conn = conn
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        if self.calls == 1:
            current = get_note(self.conn, VAULT, "n-1", space="dev")
            update_note_if(
                self.conn, VAULT, "n-1", current.updated, {"body": "新版"}, space="dev"
            )
        return VEC


def test_race_embedding_for_old_version_is_discarded(conn, add_note, clock):
    add_note("n-1", body="舊版")
    embedder = UpdatingEmbedder(conn)
    worker = make_worker(conn, clock, embed=embedder)
    stats = worker.run_once()
    assert stats.embedding.stale == 1
    assert vectors.get_embedding(conn, VAULT, "n-1", space="dev") is None
    assert worker.run_once().embedding.done == 1
    assert vectors.get_embedding(conn, VAULT, "n-1", space="dev") is not None


def test_write_summary_if_current_refuses_stale_version(conn, add_note):
    """拿掉 `updated` 比對時這個測試會紅：舊版本的摘要不可寫入。"""
    note = add_note("n-1")
    seq = conn.execute("SELECT seq FROM notes WHERE id = 'n-1'").fetchone()[0]
    update_note_if(conn, VAULT, "n-1", note.updated, {"body": "改過"}, space="dev")
    assert store.write_summary_if_current(conn, seq, note.updated, "舊摘要") is False
    assert get_note(conn, VAULT, "n-1", space="dev").summary is None


def test_new_version_resets_failed_state(conn, add_note, clock, http):
    add_note("n-1")
    fake = http.Transport(http.chat(""), http.chat("新版摘要"))
    worker = make_worker(conn, clock, summary=fake, max_attempts=1)
    assert worker.run_once().summary.gave_up == 1
    assert doctor(conn, now=clock())["enrich.failed"].status is Status.FAIL

    current = get_note(conn, VAULT, "n-1", space="dev")
    update_note_if(conn, VAULT, "n-1", current.updated, {"body": "改寫後"}, space="dev")
    # 失敗紀錄屬於舊版本，不再算失敗；新版本重新排入
    assert doctor(conn, now=clock())["enrich.failed"].status is Status.PASS
    assert worker.run_once().summary.done == 1


def test_summary_writeback_keeps_list_order(conn, add_note, clock, http):
    add_note("old", ts="2026-09-01T00:00:00.000Z")
    add_note("new", ts="2026-09-01T01:00:00.000Z", summary="已有")
    make_worker(conn, clock, summary=http.Transport(http.chat("補上"))).run_once()
    page, _ = list_notes(conn, VAULT, space="dev")
    assert [n.id for n in page] == ["new", "old"]


# ── 限速 ──


class MonotonicClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_rate_limiter_spaces_calls():
    mono = MonotonicClock()
    limiter = RateLimiter(30, clock=mono, sleep=mono.sleep)
    for _ in range(3):
        limiter.acquire()
    assert mono.sleeps == [2.0, 2.0]
    # 呼叫本身花掉的時間要扣掉
    mono.t += 1.5
    limiter.acquire()
    assert mono.sleeps[-1] == pytest.approx(0.5)
    unlimited = RateLimiter(0, clock=mono, sleep=mono.sleep)
    unlimited.acquire()
    unlimited.acquire()
    assert len(mono.sleeps) == 3


def test_worker_applies_summary_rate_limit(conn, add_note, clock, http):
    for i in range(3):
        add_note(f"n-{i}")
    mono = MonotonicClock()
    fake = http.Transport(*(http.chat(f"摘要 {i}") for i in range(3)))
    worker = EnrichWorker(
        conn,
        WorkerConfig(),
        embedder=None,
        summarizer=OpenAISummarizer(SummaryConfig(), Secret(KEY), transport=fake),
        summary_limiter=RateLimiter(20, clock=mono, sleep=mono.sleep),
        now=clock,
    )
    assert worker.run_once().summary.done == 3
    assert mono.sleeps == [3.0, 3.0]


def test_batch_limit(conn, add_note, clock, http):
    for i in range(3):
        add_note(f"n-{i}")
    fake = http.Transport(http.chat("a"), http.chat("b"))
    worker = make_worker(conn, clock, summary=fake, batch_size=2)
    assert worker.run_once().summary.done == 2


# ── doctor：積壓 ──


def test_backlog_warns_only_when_oldest_waits_too_long(conn, add_note, clock):
    assert doctor(conn, now=clock())["enrich.backlog"].status is Status.PASS
    add_note("n-1")
    # 入列時間是寫入當下的牆鐘；改成 09-01 模擬已等 1 天（clock 在 09-02）
    conn.execute(
        "UPDATE notes SET enqueued = '2026-09-01T00:00:00.000Z' WHERE id = 'n-1'"
    )
    fresh = doctor(conn, now=clock(), enrich_backlog_max_age=10**6)["enrich.backlog"]
    assert fresh.status is Status.PASS
    assert fresh.counts["summary_pending"] == 1
    assert fresh.counts["embedding_pending"] == 1
    stale = doctor(conn, now=clock(), enrich_backlog_max_age=3600)["enrich.backlog"]
    assert stale.status is Status.WARN
    assert stale.counts["oldest_age_seconds"] == 86400


def test_backlog_age_uses_enqueue_time_not_old_updated(conn, add_note):
    """舊 PM 匯入的 note 保留一年前的 `updated`；入列時間要是寫入當下，
    否則 backlog 會回報一年的等待時間並 warn。"""
    from datetime import UTC, datetime

    note = add_note("n-old", ts="2025-09-01T00:00:00.000Z")
    result = doctor(conn, now=datetime.now(UTC), enrich_backlog_max_age=3600)
    backlog = result["enrich.backlog"]
    assert backlog.counts["summary_pending"] == 1
    assert backlog.counts["oldest_age_seconds"] < 60
    assert backlog.status is Status.PASS
    # 以舊時間（`now=`，匯入更新的寫法）更新：新版本仍以牆鐘重新入列
    updated = update_note_if(
        conn,
        VAULT,
        "n-old",
        note.updated,
        {"body": "新內文"},
        space="dev",
        now="2025-09-02T00:00:00.000Z",
    )
    assert updated is not None and updated.updated.startswith("2025-09-02")
    again = doctor(conn, now=datetime.now(UTC), enrich_backlog_max_age=3600)
    assert again["enrich.backlog"].counts["oldest_age_seconds"] < 60
    assert again["enrich.queue_time"].status is Status.PASS


def test_queue_time_check_fails_when_enqueued_missing(conn, add_note):
    """寫入路徑漏填 enqueued 時 min() 會默默略過；doctor 必須紅。"""
    add_note("n-1")
    assert doctor(conn)["enrich.queue_time"].status is Status.PASS
    conn.execute("UPDATE notes SET enqueued = NULL WHERE id = 'n-1'")
    result = doctor(conn)["enrich.queue_time"]
    assert result.status is Status.FAIL
    assert result.counts["missing_enqueued"] == 1


def test_enrich_checks_skip_on_unmigrated_db(tmp_path):
    raw = sqlite3.connect(tmp_path / "v1.db", isolation_level=None)
    try:
        migrate(raw, migrations=MIGRATIONS[:1])
        results = doctor(raw)
        assert results["enrich.failed"].status is Status.SKIPPED
        assert results["enrich.backlog"].status is Status.SKIPPED
        assert results["enrich.queue_time"].status is Status.SKIPPED
    finally:
        raw.close()


def test_v1_database_upgrades_to_enrichment_schema(tmp_path):
    from lore_vault.storage.db import connect

    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path, isolation_level=None)
    migrate(raw, migrations=MIGRATIONS[:1])
    raw.close()
    upgraded = connect(path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert upgraded.execute("SELECT count(*) FROM note_enrichment").fetchone()
    finally:
        upgraded.close()


def test_enrichment_rows_cascade_on_note_delete(conn, add_note, clock, http):
    from lore_vault.storage.notes import delete_note

    add_note("n-1")
    make_worker(conn, clock, summary=http.Transport(http.chat(""))).run_once()
    assert len(enrichment_rows(conn)) == 1
    delete_note(conn, VAULT, "n-1", space="dev")
    assert enrichment_rows(conn) == []


# ── CLI ──


def test_cli_once_with_fake_transport(tmp_path, conn, add_note, http, monkeypatch):
    add_note("n-1")
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("LORE_VAULT_EMBEDDING_DIM", str(DIM))
    monkeypatch.delenv("LORE_VAULT_CONFIG", raising=False)

    def route(url, body, headers, timeout):
        if url.endswith("/chat/completions"):
            return http.chat("CLI 摘要")
        return http.embed(VEC)

    out = io.StringIO()
    code = enrich_main(["--once", "--db", db_path], transport=route, stdout=out)
    assert code == 0
    text = out.getvalue()
    assert KEY not in text
    stats = json.loads(text.splitlines()[-1])
    assert stats["summary"]["done"] == 1 and stats["embedding"]["done"] == 1
    assert get_note(conn, VAULT, "n-1", space="dev").summary == "CLI 摘要"


def test_cli_without_db_path_is_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("LORE_VAULT_DATABASE_PATH", raising=False)
    monkeypatch.delenv("LORE_VAULT_CONFIG", raising=False)
    assert enrich_main(["--once"], transport=lambda *a: None) == 2
    assert "資料庫路徑" in capsys.readouterr().err
