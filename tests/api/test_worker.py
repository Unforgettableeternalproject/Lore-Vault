"""同一程序內的背景補算 worker：可關閉、會被 write 喚醒、關閉路徑會停下。

SIGTERM → uvicorn → lifespan 結束 → `BackgroundEnricher.stop()`。
這裡驗證 lifespan 之後的這一段；訊號本身由 uvicorn 處理（Windows 上不送真訊號）。
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.api.background import BackgroundEnricher
from lore_vault.config import Config, EmbeddingConfig, WorkerConfig
from lore_vault.enrich.worker import STOPPED_SHUTDOWN, EnrichWorker
from lore_vault.schema import Note, Vault
from lore_vault.storage.db import connect
from lore_vault.storage.notes import insert_note
from lore_vault.storage.vaults import upsert_vault

from .conftest import (
    AUTH,
    DIM,
    SpaceClient,
    create_vault,
    fake_vector,
    make_settings,
    write_note,
)

TS = "2026-09-01T00:00:00.000Z"


class FakeEnrichEmbedder:
    model = "fake"
    dim = DIM

    def embed(self, text: str) -> list[float]:
        return fake_vector(text)


class FakeSummarizer:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, title: str, body: str) -> str:
        self.calls += 1
        return f"摘要：{title}"


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _config(poll_interval: float = 60.0) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        worker=WorkerConfig(poll_interval=poll_interval),
    )


def _factory(summarizer: FakeSummarizer):
    def make(conn, should_stop):
        return EnrichWorker(
            conn,
            WorkerConfig(),
            embedder=FakeEnrichEmbedder(),
            summarizer=summarizer,
            should_stop=should_stop,
        )

    return make


def test_worker_can_be_disabled(make_client):
    c = make_client(enrich_worker=False)
    assert c.app.state.lore.enricher is None
    assert c.post("/v1/status").json()["enrich"]["worker"]["enabled"] is False


def test_worker_runs_in_process_and_write_wakes_it(db_path):
    summarizer = FakeSummarizer()
    settings = make_settings(
        db_path,
        config=_config(poll_interval=60.0),
        enrich_worker=True,
        worker_factory=_factory(summarizer),
    )
    app = create_app(settings)
    with SpaceClient(app) as c:
        c.headers.update(AUTH)
        enricher = app.state.lore.enricher
        assert enricher.running
        create_vault(c, "folder/w")
        note = write_note(c, "folder/w", "標題", "正文")
        # poll_interval 60 秒：能補完只可能是 write 喚醒的
        assert _wait_until(
            lambda: (
                c.post(
                    "/v1/get", json={"vault": "folder/w", "ids": [note["id"]]}
                ).json()["items"][0]["summary_source"]
                == "summary"
            )
        )
        status = c.post("/v1/status").json()["enrich"]
        assert status["worker"]["running"] is True
        assert status["worker"]["runs"] >= 1
        assert status["worker"]["last_error"] is None
        thread = enricher._thread
    # 離開 TestClient = lifespan shutdown（uvicorn 收到 SIGTERM 時走同一條路）
    assert not thread.is_alive()
    assert not enricher.running


def test_stop_interrupts_poll_wait_immediately(db_path):
    connect(db_path).close()
    ran = threading.Event()

    class Idle:
        def run_once(self):
            ran.set()
            return _Stats()

    enricher = BackgroundEnricher(
        db_path, lambda conn, stop: Idle(), poll_interval=3600.0
    )
    enricher.start()
    assert ran.wait(5)
    started = time.monotonic()
    assert enricher.stop(timeout=5) is True
    assert time.monotonic() - started < 2
    assert not enricher.running


def test_round_failure_is_logged_and_thread_keeps_running(db_path):
    connect(db_path).close()
    attempts = []

    class Flaky:
        def run_once(self):
            attempts.append(1)
            raise RuntimeError("boom")

    enricher = BackgroundEnricher(db_path, lambda conn, stop: Flaky(), poll_interval=60)
    enricher.start()
    try:
        assert _wait_until(lambda: len(attempts) >= 1)
        enricher.wake()
        assert _wait_until(lambda: len(attempts) >= 2)
        status = enricher.status()
        assert status["running"] is True
        assert "RuntimeError: boom" in status["last_error"]
    finally:
        assert enricher.stop(timeout=5)


def test_factory_failure_is_visible_in_status(db_path):
    connect(db_path).close()

    def broken(conn, stop):
        raise RuntimeError("cannot build")

    enricher = BackgroundEnricher(db_path, broken, poll_interval=60)
    enricher.start()
    assert _wait_until(lambda: not enricher.running)
    assert "cannot build" in enricher.status()["fatal_error"]
    assert enricher.stop(timeout=1)


def test_should_stop_aborts_mid_batch(tmp_path):
    """關閉訊號在一輪中途到達：下一則 note 前就停，不把整批跑完。"""
    conn = connect(tmp_path / "lore.db")
    try:
        upsert_vault(conn, Vault(key="folder/s", display="s"))
        for i in range(5):
            insert_note(
                conn,
                "folder/s",
                Note(
                    id=f"n{i}",
                    vault="folder/s",
                    title=f"t{i}",
                    body="b",
                    created=TS,
                    updated=TS,
                ),
                space="dev",
            )
        stop = threading.Event()

        class StopAfterOne(FakeSummarizer):
            def summarize(self, title, body):
                stop.set()
                return super().summarize(title, body)

        summarizer = StopAfterOne()
        worker = EnrichWorker(
            conn,
            WorkerConfig(),
            embedder=FakeEnrichEmbedder(),
            summarizer=summarizer,
            should_stop=stop.is_set,
        )
        stats = worker.run_once()
        assert summarizer.calls == 1
        assert stats.summary.done == 1
        assert stats.summary.stopped == STOPPED_SHUTDOWN
        assert stats.embedding.done == 0
    finally:
        conn.close()


class _Stats:
    def to_dict(self):
        return {}


def test_status_not_ok_when_worker_cannot_start(db_path):
    def broken(conn, stop):
        raise RuntimeError("cannot build")

    app = create_app(
        make_settings(
            db_path, config=_config(), enrich_worker=True, worker_factory=broken
        )
    )
    with TestClient(app) as c:
        c.headers.update(AUTH)
        assert _wait_until(lambda: not app.state.lore.enricher.running)
        data = c.post("/v1/status").json()
        assert "cannot build" in data["enrich"]["worker"]["fatal_error"]
        assert data["ok"] is False
