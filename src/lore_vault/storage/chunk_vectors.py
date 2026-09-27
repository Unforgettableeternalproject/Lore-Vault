"""文件 chunk 的向量（T-64）：寫入、暴力比對、缺向量計數。

比照 `storage.vectors`（float32 BLOB、寫入前 L2 正規化、先套範圍再全量點積），
但用獨立的 `document_chunk_embeddings` 表（設計 3.1），只收可索引文件
（ready、未被取代）的 chunk。

本模組 import numpy；hook 路徑不得 import storage。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .documents import eligible_clause
from .timeutil import utc_now
from .vaults import resolve_read, vault_clause
from .vectors import _DTYPE, _validate_dim, normalize


def set_chunk_embedding(
    conn: sqlite3.Connection,
    chunk_seq: int,
    vector: Sequence[float] | np.ndarray,
    *,
    dim: int,
    model: str | None = None,
) -> None:
    """寫入／覆蓋一個 chunk 的 embedding。呼叫端負責交易與「仍可索引」的確認。"""
    normalized = normalize(vector, dim)
    conn.execute(
        """
        INSERT INTO document_chunk_embeddings (chunk_seq, dim, vector, model, updated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (chunk_seq) DO UPDATE SET dim = excluded.dim,
            vector = excluded.vector, model = excluded.model,
            updated = excluded.updated
        """,
        (chunk_seq, dim, normalized.tobytes(), model, utc_now()),
    )


@dataclass(frozen=True)
class ChunkVectorHit:
    document_id: str
    idx: int
    vault: str
    # cosine 相似度（-1–1）
    score: float


def search_chunk_vectors(
    conn: sqlite3.Connection,
    vault: str,
    query: Sequence[float] | np.ndarray,
    *,
    space: str,
    dim: int,
    limit: int = 20,
) -> list[ChunkVectorHit]:
    """在 vault（與 space）範圍內、可索引文件的 chunk 向量做點積，回傳前 `limit` 名。"""
    scope = resolve_read(conn, vault, space=space)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    q = normalize(query, dim)
    clause, params = vault_clause(scope, "d.vault")
    rows = conn.execute(
        f"""
        SELECT c.document_id, c.idx, d.vault, e.vector
        FROM document_chunk_embeddings e
        JOIN document_chunks c ON c.seq = e.chunk_seq
        JOIN documents d ON d.id = c.document_id
        WHERE e.dim = ? AND length(e.vector) = ? AND {clause}
          AND {eligible_clause("d")}
        """,
        (dim, dim * _DTYPE.itemsize, *params),
    ).fetchall()
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r[3] for r in rows), dtype=_DTYPE).reshape(
        len(rows), dim
    )
    scores = matrix @ q
    k = min(limit, len(rows))
    top = np.argpartition(-scores, k - 1)[:k]
    order = sorted(
        top.tolist(), key=lambda i: (-float(scores[i]), rows[i][0], int(rows[i][1]))
    )
    return [
        ChunkVectorHit(rows[i][0], int(rows[i][1]), rows[i][2], float(scores[i]))
        for i in order
    ]


def count_chunks_without_vector(
    conn: sqlite3.Connection, vault: str, *, space: str, dim: int
) -> int:
    """範圍內可索引文件中沒有可用向量的 chunk 數（recall 告知向量那一路不完整）。"""
    _validate_dim(dim)
    scope = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope, "d.vault")
    return int(
        conn.execute(
            f"""
            SELECT count(*) FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {clause} AND {eligible_clause("d")} AND NOT EXISTS (
                SELECT 1 FROM document_chunk_embeddings e
                WHERE e.chunk_seq = c.seq AND e.dim = ? AND length(e.vector) = ?
            )
            """,
            (*params, dim, dim * _DTYPE.itemsize),
        ).fetchone()[0]
    )
