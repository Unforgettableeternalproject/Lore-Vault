"""T-63／T-64：文件 worker（pending → extracting → ready／failed）。

涵蓋 chunk 索引、向量補算、版本切換的索引資格。不打網路：embedder 是假的，
時鐘可控。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, WorkerConfig
from lore_vault.documents import service as doc_service
from lore_vault.documents import worker as worker_mod
from lore_vault.documents.worker import DocumentWorker, has_activity, progress_line
from lore_vault.enrich.clients import InvalidOutput, ProviderUnavailable
from lore_vault.schema import Vault
from lore_vault.storage import document_index as index
from lore_vault.storage import fts
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.chunk_vectors import search_chunk_vectors
from lore_vault.storage.db import connect
from lore_vault.storage.documents import get_document, superseded_by
from lore_vault.storage.vaults import upsert_vault

VAULT = "folder/docs"
DIM = 8


def _vec(text: str) -> list[float]:
    vec = np.zeros(DIM)
    for token in fts.tokens(text):
        vec[int(hashlib.sha1(token.lower().encode()).hexdigest(), 16) % DIM] += 1.0
    if not vec.any():
        vec[0] = 1.0
    return vec.tolist()


class FakeEmbedder:
    model = "fake-bge"

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return _vec(text)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lore.db")
    upsert_vault(connection, Vault(key=VAULT, display="docs", kind="repo"))
    yield connection
    connection.close()


@pytest.fixture
def blobs(tmp_path):
    return BlobStore(tmp_path / "blobs")


@pytest.fixture
def config(tmp_path):
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        worker=WorkerConfig(max_attempts=2, retry_backoff=10.0),
        documents=DocumentsConfig(blob_dir=str(tmp_path / "blobs")),
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_worker(conn, blobs, config, clock):
    def make(embedder=None, **kw):
        return DocumentWorker(
            conn,
            config,
            blobs=blobs,
            embedder=FakeEmbedder() if embedder is None else embedder,
            now=clock,
            **kw,
        )

    return make


@pytest.fixture
def upload(conn, blobs):
    def up(data: bytes, filename: str, vault: str = VAULT, space: str = "dev"):
        return doc_service.upload(
            conn,
            blobs,
            vault,
            data,
            space=space,
            filename=filename,
            max_bytes=25 * 1024 * 1024,
        )

    return up


def _doc(conn, doc_id):
    return get_document(conn, VAULT, doc_id, space="dev")


def _fts_rows(conn, doc_id) -> int:
    return conn.execute(
        "SELECT count(*) FROM chunk_fts WHERE rowid IN "
        "(SELECT seq FROM document_chunks WHERE document_id = ?)",
        (doc_id,),
    ).fetchone()[0]


def _vector_rows(conn, doc_id) -> int:
    return conn.execute(
        "SELECT count(*) FROM document_chunk_embeddings WHERE chunk_seq IN "
        "(SELECT seq FROM document_chunks WHERE document_id = ?)",
        (doc_id,),
    ).fetchone()[0]


LONG_MD = "# 設定\n\n" + "".join(
    f"第{i}段說明資料庫遷移與備份的流程。" for i in range(60)
)


# ── 抽取與索引 ──────────────────────────────────────────────────────


def test_pending_becomes_ready_with_chunks_fts_and_vectors(conn, upload, make_worker):
    doc = upload(LONG_MD.encode(), "設定.md").document
    assert doc.status == "pending"
    stats = make_worker().run_once()
    assert stats.extract.done == 1 and stats.embedding.done > 1
    ready = _doc(conn, doc.id)
    assert ready.status == "ready" and ready.encoding == "utf-8"
    chunks = index.chunks_of(conn, doc.id)
    assert ready.chunk_count == len(chunks) > 1
    assert chunks[0].locator["kind"] == "heading" and chunks[0].locator["part"] == 1
    assert _fts_rows(conn, doc.id) == len(chunks) == _vector_rows(conn, doc.id)
    hits = fts.search_chunks(conn, VAULT, "資料庫遷移", space="dev")
    assert {h.document_id for h in hits} == {doc.id}
    vec_hits = search_chunk_vectors(
        conn, VAULT, _vec("資料庫遷移"), space="dev", dim=DIM
    )
    assert vec_hits and vec_hits[0].document_id == doc.id
    # 每個 chunk 的向量都記了模型
    models = {r[0] for r in conn.execute("SELECT model FROM document_chunk_embeddings")}
    assert models == {"fake-bge"}


def test_big5_document_records_cp950(conn, upload, make_worker):
    doc = upload("繁體中文的世界觀設定說明文件".encode("cp950"), "世界觀.txt").document
    make_worker().run_once()
    ready = _doc(conn, doc.id)
    assert ready.encoding == "cp950"
    # 樣本只有 14 個非 ASCII 字元：判定信心低，記在 warnings（doctor 會列出）
    assert [w["code"] for w in ready.warnings] == ["encoding_low_confidence"]
    assert index.quality_warnings(conn).status == "warn"


def test_extraction_error_fails_immediately_without_retry(conn, upload, make_worker):
    doc = upload(b"PK\x03\x04 not really a zip", "壞掉.docx").document
    stats = make_worker().run_once()
    assert stats.extract.failed == 1
    failed = _doc(conn, doc.id)
    assert failed.status == "failed" and failed.error_code == "corrupt"
    assert failed.error_detail
    assert index.extract_candidates(conn, now="9999", limit=10) == []


def test_unexpected_error_retries_with_backoff_then_gives_up(
    conn, upload, make_worker, clock, monkeypatch
):
    doc = upload(b"hello world", "a.txt").document

    def boom(*args, **kwargs):
        raise RuntimeError("解析套件爆炸")

    monkeypatch.setattr(worker_mod, "extract", boom)
    worker = make_worker()
    first = worker.run_once()
    assert first.extract.retry == 1
    assert _doc(conn, doc.id).status == "pending"
    # 退避期間不重試
    assert worker.run_once().extract.retry == 0
    clock.advance(11)
    second = worker.run_once()
    assert second.extract.gave_up == 1
    failed = _doc(conn, doc.id)
    assert failed.status == "failed" and failed.error_code == "corrupt"
    assert "解析套件爆炸" in failed.error_detail


def test_missing_blob_is_retried_then_marked_corrupt(
    conn, upload, make_worker, blobs, clock
):
    doc = upload(b"hello world", "a.txt").document
    blobs.path_for(doc.sha256).unlink()
    worker = make_worker()
    assert worker.run_once().extract.retry == 1
    clock.advance(11)
    assert worker.run_once().extract.gave_up == 1
    failed = _doc(conn, doc.id)
    assert failed.error_code == "corrupt" and "blob" in failed.error_detail


def test_interrupted_extraction_is_recovered_on_first_round(conn, upload, make_worker):
    doc = upload(b"hello world", "a.txt").document
    assert index.claim(conn, doc.id)
    assert _doc(conn, doc.id).status == "extracting"
    stats = make_worker().run_once()
    assert stats.recovered == 1 and stats.extract.done == 1
    assert _doc(conn, doc.id).status == "ready"


def test_should_stop_ends_round_between_documents(conn, upload, make_worker):
    upload(b"one", "1.txt")
    upload(b"two", "2.txt")
    stats = make_worker(should_stop=lambda: True).run_once()
    assert stats.extract.stopped and stats.extract.done == 0


# ── 向量補算 ───────────────────────────────────────────────────────


def test_provider_unavailable_stops_without_consuming_attempts(
    conn, upload, make_worker
):
    doc = upload(b"hello world", "a.txt").document
    stats = make_worker(FakeEmbedder(ProviderUnavailable("Ollama 連不上"))).run_once()
    assert stats.extract.done == 1 and "Ollama" in stats.embedding.stopped
    assert conn.execute("SELECT count(*) FROM document_enrichment").fetchone()[0] == 0
    assert _fts_rows(conn, doc.id) == 1 and _vector_rows(conn, doc.id) == 0


def test_embedding_failures_are_capped_per_document(conn, upload, make_worker, clock):
    doc = upload(LONG_MD.encode(), "a.md").document
    bad = make_worker(FakeEmbedder(InvalidOutput("空向量")))
    first = bad.run_once()
    # 同一份文件本輪只試一次（其餘 chunk 略過），不會一輪燒光嘗試次數
    assert first.embedding.retry == 1
    clock.advance(11)
    assert bad.run_once().embedding.gave_up == 1
    assert index.embedding_candidates(conn, now="9999", limit=100) == []
    rec = index.failed_documents(conn)
    assert rec.status == "warn" and rec.counts["embedding_failed"] == 1
    # 人工 reset 後可重試
    assert index.reset_failed_embeddings(conn) == 1
    assert make_worker().run_once().embedding.done == len(index.chunks_of(conn, doc.id))


def test_progress_line_and_activity():
    stats = {
        "extract": {"done": 2, "failed": 1, "retry": 0, "gave_up": 0},
        "embedding": {"done": 5, "retry": 1, "gave_up": 0},
        "recovered": 0,
    }
    line = progress_line(stats, (3, 7))
    assert "抽取 完成 2／失敗 1" in line and "向量 完成 5／重試 1" in line
    assert "待抽取 3、缺向量 chunk 7" in line
    idle = {"extract": {"done": 0}, "embedding": {"done": 0}, "recovered": 0}
    assert has_activity(stats) and not has_activity(idle)


# ── 版本與索引資格 ─────────────────────────────────────────────────


def test_new_version_deindexes_old_only_when_ready(conn, upload, make_worker):
    v1 = upload("第一版：舊的設定值".encode(), "設定.md").document
    make_worker().run_once()
    v2 = upload("第二版：新的設定值".encode(), "設定.md").document
    assert v2.supersedes == v1.id and v2.version == 2
    # 新版還沒 ready：舊版仍在索引（中間不會有誰都搜不到的空窗）
    assert _fts_rows(conn, v1.id) == 1 and superseded_by(conn, [v1.id]) == {}
    make_worker().run_once()
    assert _fts_rows(conn, v1.id) == 0 and _vector_rows(conn, v1.id) == 0
    assert _fts_rows(conn, v2.id) == 1
    assert superseded_by(conn, [v1.id]) == {v1.id: v2.id}
    # 舊版的 chunk 仍在（get 查得到）
    assert len(index.chunks_of(conn, v1.id)) == 1
    assert index.superseded_chunks_removed(conn).status == "pass"


def test_superseded_through_failed_version_is_not_left_indexed(
    conn, upload, make_worker, clock, monkeypatch
):
    """v1 ready → v2 抽取失敗 → v3 ready：v1 必須退出索引（遞移取代）。"""
    v1 = upload("第一版內容".encode(), "設定.txt").document
    make_worker().run_once()
    real_extract = worker_mod.extract

    def fail_second(data, filename, mime, **kw):
        if data == "第二版內容".encode():
            from lore_vault.documents.extract import ExtractionError

            raise ExtractionError("corrupt", "模擬失敗")
        return real_extract(data, filename, mime, **kw)

    monkeypatch.setattr(worker_mod, "extract", fail_second)
    v2 = upload("第二版內容".encode(), "設定.txt").document
    make_worker().run_once()
    assert _doc(conn, v2.id).status == "failed"
    v3 = upload("第三版內容".encode(), "設定.txt").document
    # 前一個現行版本是 v1（v2 失敗），版本號接在最大版本之後
    assert v3.supersedes == v1.id and v3.version == 3
    make_worker().run_once()
    assert _fts_rows(conn, v1.id) == 0 and _fts_rows(conn, v3.id) == 1
    assert superseded_by(conn, [v1.id]) == {v1.id: v3.id}


def test_late_older_version_does_not_enter_index(conn, upload, make_worker):
    """v2 還在 pending 時就傳了 v3 且 v3 先 ready：v2 之後 ready 也不能進索引。"""
    v1 = upload("一".encode(), "a.txt").document
    make_worker().run_once()
    v2 = upload("二".encode(), "a.txt").document
    v3 = upload("三".encode(), "a.txt").document
    assert v3.supersedes == v2.id
    # 只處理 v3
    assert index.claim(conn, v3.id)
    from lore_vault.documents.chunking import chunk_segments
    from lore_vault.documents.extract import extract

    result = extract("三".encode(), "a.txt")
    index.finish_ready(conn, v3.id, chunk_segments(result.segments), encoding="utf-8")
    make_worker().run_once()  # 處理 v2
    assert _doc(conn, v2.id).status == "ready"
    assert _fts_rows(conn, v1.id) == 0 and _fts_rows(conn, v2.id) == 0
    assert _fts_rows(conn, v3.id) == 1
    assert index.fts_rows_match_chunks(conn).status == "pass"


# ── 子行程隔離（逾時）─────────────────────────────────────────────


def test_extraction_timeout_fails_document_and_queue_continues(
    conn, upload, make_worker, make_pdf, tmp_path, monkeypatch
):
    """卡住的 pdf 在逾時後被 kill、標 corrupt（detail 註明 timeout），
    同一輪的下一份照常處理。卡住以子行程內的 `time.sleep` 模擬。"""
    import time

    config = Config(
        embedding=EmbeddingConfig(dim=DIM),
        worker=WorkerConfig(max_attempts=2, retry_backoff=10.0),
        documents=DocumentsConfig(
            blob_dir=str(tmp_path / "blobs"), extract_timeout=1.0
        ),
    )
    real_run = worker_mod.run_isolated
    calls = []

    def run(func, data, filename, mime, **kw):
        calls.append((filename, kw["timeout"]))
        if filename == "slow.pdf":
            return real_run(time.sleep, 3600, timeout=kw["timeout"])
        return real_run(func, data, filename, mime, **kw)

    monkeypatch.setattr(worker_mod, "run_isolated", run)
    slow = upload(make_pdf(["slow document " * 10]), "slow.pdf").document
    good = upload(make_pdf(["normal document content " * 5]), "good.pdf").document
    started = time.monotonic()
    worker = make_worker()
    worker.config = config
    stats = worker.run_once()
    elapsed = time.monotonic() - started
    assert [name for name, _ in calls] == ["slow.pdf", "good.pdf"]
    assert all(timeout == 1.0 for _, timeout in calls)
    failed = _doc(conn, slow.id)
    assert failed.status == "failed" and failed.error_code == "corrupt"
    assert "timeout" in failed.error_detail
    assert _doc(conn, good.id).status == "ready"
    assert stats.extract.failed == 1 and stats.extract.done == 1
    assert elapsed < 60


def test_binary_formats_run_isolated_text_formats_in_process(
    conn, upload, make_worker, make_pdf, monkeypatch
):
    seen = []
    real_run = worker_mod.run_isolated

    def run(func, data, filename, mime, **kw):
        seen.append(filename)
        return real_run(func, data, filename, mime, **kw)

    monkeypatch.setattr(worker_mod, "run_isolated", run)
    upload(make_pdf(["isolated pdf content here " * 4]), "a.pdf")
    upload(b"plain text body", "b.txt")
    stats = make_worker().run_once()
    assert stats.extract.done == 2
    assert seen == ["a.pdf"]


def test_isolated_child_crash_goes_through_retry(
    conn, upload, make_worker, monkeypatch
):
    import os

    real_run = worker_mod.run_isolated

    def run(func, data, filename, mime, **kw):
        return real_run(os._exit, 7, timeout=kw["timeout"])

    monkeypatch.setattr(worker_mod, "run_isolated", run)
    doc = upload(b"%PDF-1.4 whatever", "crash.pdf").document
    stats = make_worker().run_once()
    assert stats.extract.retry == 1
    assert _doc(conn, doc.id).status == "pending"
