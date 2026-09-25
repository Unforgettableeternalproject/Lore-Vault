"""T-18：向量正規化、維度檢查、先過濾後比對、效能量測。"""

from __future__ import annotations

import time

import numpy as np
import pytest

from lore_vault.storage import notes, vectors
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import DimensionMismatch

DIM = 8


@pytest.fixture
def vault(add_vault):
    return add_vault("folder/vec")


def test_stored_vector_is_l2_normalized(conn, vault, add_note):
    add_note(vault, "n-1", "t")
    raw = np.full(DIM, 25.0 / np.sqrt(DIM))  # norm 25，模擬 Ollama 原始輸出
    vectors.set_embedding(conn, vault, "n-1", raw, dim=DIM, model="bge-m3")
    stored = vectors.get_embedding(conn, vault, "n-1")
    assert stored is not None and stored.dtype == np.float32
    assert np.linalg.norm(stored) == pytest.approx(1.0, abs=1e-6)
    blob = conn.execute("SELECT vector FROM note_embeddings").fetchone()[0]
    assert len(blob) == DIM * 4


@pytest.mark.parametrize(
    "bad",
    [[1.0] * (DIM - 1), [1.0] * (DIM + 1), [0.0] * DIM, [float("nan")] + [1.0] * 7],
    ids=["short", "long", "zero", "nan"],
)
def test_invalid_vectors_are_rejected(conn, vault, add_note, bad):
    add_note(vault, "n-1", "t")
    with pytest.raises(DimensionMismatch):
        vectors.set_embedding(conn, vault, "n-1", bad, dim=DIM)
    with pytest.raises(DimensionMismatch):
        vectors.search_vectors(conn, vault, bad, dim=DIM)
    assert conn.execute("SELECT count(*) FROM note_embeddings").fetchone()[0] == 0


def test_dim_comes_from_caller_not_hardcoded(conn, vault, add_note):
    add_note(vault, "n-1", "t")
    vectors.set_embedding(conn, vault, "n-1", [1, 2, 3], dim=3)
    assert [
        h.note_id for h in vectors.search_vectors(conn, vault, [1, 2, 3], dim=3)
    ] == ["n-1"]
    with pytest.raises(DimensionMismatch):
        vectors.search_vectors(conn, vault, [1, 2, 3], dim=0)


def test_search_orders_by_cosine(conn, vault, add_note):
    for i, vec in enumerate(([1, 0], [1, 1], [0, 1], [-1, 0])):
        add_note(vault, f"n-{i}", "t")
        vectors.set_embedding(conn, vault, f"n-{i}", vec, dim=2)
    hits = vectors.search_vectors(conn, vault, [1, 0.1], dim=2, limit=3)
    assert [h.note_id for h in hits] == ["n-0", "n-1", "n-2"]
    assert hits[0].score == pytest.approx(1 / np.sqrt(1.01), abs=1e-6)


def test_vault_filter_happens_before_top_k(conn, add_vault, add_note):
    """B 有 50 條幾乎相同的向量、A 只有一條普通相似的；搜 A 仍必須拿到 A 的那條。

    若是「先全庫取 top-k 再過濾」，A 的那條會被 B 擠出前 k 名而靜默漏掉。
    """
    a, b = add_vault("folder/a"), add_vault("folder/b")
    add_note(a, "a-1", "t")
    vectors.set_embedding(conn, a, "a-1", [0.3, 1.0], dim=2)
    for i in range(50):
        add_note(b, f"b-{i}", "t")
        vectors.set_embedding(conn, b, f"b-{i}", [1.0, 0.001 * i], dim=2)
    hits = vectors.search_vectors(conn, a, [1.0, 0.0], dim=2, limit=5)
    assert [h.note_id for h in hits] == ["a-1"]


def test_rows_with_other_dim_do_not_participate(conn, vault, add_note):
    add_note(vault, "n-ok", "t")
    add_note(vault, "n-old", "t")
    vectors.set_embedding(conn, vault, "n-ok", [1] * DIM, dim=DIM)
    vectors.set_embedding(conn, vault, "n-old", [1, 1, 1], dim=3)  # 換模型前的殘留
    hits = vectors.search_vectors(conn, vault, [1] * DIM, dim=DIM)
    assert [h.note_id for h in hits] == ["n-ok"]


def test_content_change_drops_stale_embedding(conn, vault, add_note):
    note = add_note(vault, "n-1", "t", "body")
    vectors.set_embedding(conn, vault, "n-1", [1] * DIM, dim=DIM)
    note = notes.update_note_if(conn, vault, "n-1", note.updated, {"topics": ["x"]})
    assert vectors.get_embedding(conn, vault, "n-1") is not None  # 只改 topics 保留
    notes.update_note_if(conn, vault, "n-1", note.updated, {"body": "new body"})
    assert vectors.get_embedding(conn, vault, "n-1") is None


def test_delete_note_cascades_embedding(conn, vault, add_note):
    add_note(vault, "n-1", "t")
    vectors.set_embedding(conn, vault, "n-1", [1] * DIM, dim=DIM)
    notes.delete_note(conn, vault, "n-1")
    assert conn.execute("SELECT count(*) FROM note_embeddings").fetchone()[0] == 0


def test_brute_force_latency_thousands_x_1024(conn, vault):
    """數千條 × 1024 維的查詢耗時（D1 預期毫秒級）。門檻寬鬆，只防數量級退化。"""
    n, dim = 5000, 1024
    rng = np.random.default_rng(0)
    data = rng.standard_normal((n, dim)).astype(np.float32)
    ts = "2026-09-01T00:00:00.000Z"
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO notes (id, vault, title, body, created, updated)
            VALUES (?, ?, 't', '', ?, ?)
            """,
            [(f"n-{i}", vault, ts, ts) for i in range(n)],
        )
        for i in range(n):
            vectors.set_embedding(conn, vault, f"n-{i}", data[i], dim=dim)
    query = data[123]
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        hits = vectors.search_vectors(conn, vault, query, dim=dim, limit=10)
        timings.append(time.perf_counter() - start)
    assert hits[0].note_id == "n-123"
    median_ms = sorted(timings)[2] * 1000
    print(f"\n[perf] {n} x {dim} 暴力比對 median {median_ms:.1f} ms")
    assert median_ms < 1000
