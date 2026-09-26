"""T-63～T-67 doctor（分類 documents）：健康時綠，破壞資料後各項變紅／黃。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lore_vault.doctor import DoctorContext
from lore_vault.doctor.builtin import default_registry
from lore_vault.documents.chunking import Chunk
from lore_vault.schema import Vault
from lore_vault.storage import document_index as index
from lore_vault.storage.chunk_vectors import set_chunk_embedding
from lore_vault.storage.documents import insert_document
from lore_vault.storage.timeutil import parse_utc
from lore_vault.storage.vaults import upsert_vault

VAULT = "folder/docs"
DIM = 4
CHECKS = (
    "documents.chunk_count_matches",
    "documents.fts_rows_match_chunks",
    "documents.superseded_chunks_removed",
    "documents.vector_rows_match_chunks",
    "documents.stuck_processing",
    "documents.failed",
    "documents.backlog",
)


def _ready(conn, filename: str, texts: list[str], *, sha: str, supersedes=None):
    doc = insert_document(
        conn,
        VAULT,
        space="dev",
        filename=filename,
        mime="text/plain",
        size_bytes=1,
        sha256=sha,
        supersedes=supersedes,
    )
    assert index.claim(conn, doc.id)
    chunks = [
        Chunk(i, text, {"kind": "offset", "value": i * 10})
        for i, text in enumerate(texts)
    ]
    assert index.finish_ready(conn, doc.id, chunks, encoding="utf-8")
    for chunk in index.chunks_of(conn, doc.id):
        set_chunk_embedding(conn, chunk.seq, [1.0, 0.0, 0.0, 0.0], dim=DIM)
    return doc


@pytest.fixture
def healthy(conn):
    upsert_vault(conn, Vault(key=VAULT, display="docs", kind="repo"))
    v1 = _ready(conn, "a.txt", ["舊版內容"], sha="a" * 64)
    v2 = _ready(conn, "a.txt", ["新版一", "新版二"], sha="b" * 64, supersedes=v1.id)
    other = _ready(conn, "b.txt", ["另一份"], sha="c" * 64)
    return conn, {"v1": v1.id, "v2": v2.id, "other": other.id}


def _now(conn) -> datetime:
    latest = conn.execute("SELECT max(updated) FROM documents").fetchone()[0]
    return parse_utc(latest) + timedelta(seconds=1)


def _run(conn, now=None, **settings):
    ctx = DoctorContext(
        settings={"embedding_dim": DIM, "now": now or _now(conn), **settings},
        resources={"db": conn},
    )
    report = default_registry().run(ctx, categories=["documents"])
    return {o.name: o.result for o in report.outcomes if o.name in CHECKS}


def _status(results, name):
    return results[name].status.value


def test_healthy_state_is_green(healthy):
    conn, _ = healthy
    results = _run(conn)
    assert {name: _status(results, name) for name in CHECKS} == dict.fromkeys(
        CHECKS, "pass"
    )


def test_chunk_count_mismatch_is_red(healthy):
    conn, ids = healthy
    conn.execute("UPDATE documents SET chunk_count = 5 WHERE id = ?", (ids["other"],))
    assert _status(_run(conn), "documents.chunk_count_matches") == "fail"


def test_missing_fts_row_is_red(healthy):
    conn, ids = healthy
    conn.execute(
        "DELETE FROM chunk_fts WHERE rowid = "
        "(SELECT seq FROM document_chunks WHERE document_id = ? AND idx = 1)",
        (ids["v2"],),
    )
    result = _run(conn)["documents.fts_rows_match_chunks"]
    assert result.status.value == "fail" and result.counts["missing"] == 1


def test_superseded_version_left_in_index_is_red(healthy):
    conn, ids = healthy
    seq = conn.execute(
        "SELECT seq, text FROM document_chunks WHERE document_id = ?", (ids["v1"],)
    ).fetchone()
    conn.execute("INSERT INTO chunk_fts (rowid, content) VALUES (?, ?)", tuple(seq))
    set_chunk_embedding(conn, seq[0], [0.0, 1.0, 0.0, 0.0], dim=DIM)
    results = _run(conn)
    assert _status(results, "documents.superseded_chunks_removed") == "fail"
    assert _status(results, "documents.fts_rows_match_chunks") == "fail"


def test_vector_rows_wrong_dim_is_red_and_missing_is_warn(healthy):
    conn, ids = healthy
    conn.execute(
        "DELETE FROM document_chunk_embeddings WHERE chunk_seq IN "
        "(SELECT seq FROM document_chunks WHERE document_id = ?)",
        (ids["other"],),
    )
    assert _status(_run(conn), "documents.vector_rows_match_chunks") == "warn"
    conn.execute("UPDATE document_chunk_embeddings SET dim = 3")
    assert _status(_run(conn), "documents.vector_rows_match_chunks") == "fail"


def test_stuck_extracting_is_red_only_after_threshold(healthy):
    conn, _ = healthy
    doc = insert_document(
        conn,
        VAULT,
        space="dev",
        filename="c.txt",
        mime="text/plain",
        size_bytes=1,
        sha256="d" * 64,
    )
    assert index.claim(conn, doc.id)
    now = _now(conn)
    assert _status(_run(conn, now=now), "documents.stuck_processing") == "pass"
    later = now + timedelta(hours=2)
    assert _status(_run(conn, now=later), "documents.stuck_processing") == "fail"
    # 門檻可設定
    assert (
        _status(
            _run(conn, now=later, documents_stuck_seconds=86400),
            "documents.stuck_processing",
        )
        == "pass"
    )


def test_failed_document_is_warn_with_error_code(healthy):
    conn, _ = healthy
    doc = insert_document(
        conn,
        VAULT,
        space="dev",
        filename="scan.pdf",
        mime="application/pdf",
        size_bytes=1,
        sha256="e" * 64,
    )
    assert index.finish_failed(conn, doc.id, "empty_extraction", "掃描件")
    result = _run(conn)["documents.failed"]
    assert result.status.value == "warn"
    assert result.counts["failed_empty_extraction"] == 1


def test_old_backlog_is_warn(healthy):
    conn, _ = healthy
    insert_document(
        conn,
        VAULT,
        space="dev",
        filename="new.txt",
        mime="text/plain",
        size_bytes=1,
        sha256="f" * 64,
    )
    now = _now(conn)
    assert _status(_run(conn, now=now), "documents.backlog") == "pass"
    later = now + timedelta(hours=2)
    result = _run(conn, now=later)["documents.backlog"]
    assert result.status.value == "warn" and result.counts["documents_pending"] == 1


def test_checks_skip_on_schema_without_documents(tmp_path):
    import sqlite3

    raw = sqlite3.connect(tmp_path / "empty.db")
    try:
        ctx = DoctorContext(settings={"now": datetime.now(UTC)}, resources={"db": raw})
        report = default_registry().run(ctx, categories=["documents"])
        statuses = {o.result.status.value for o in report.outcomes if o.name in CHECKS}
        assert statuses == {"skipped"}
    finally:
        raw.close()
