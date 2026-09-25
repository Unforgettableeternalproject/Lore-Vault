"""NumPy 向量暴力比對（T-18）。

- 向量存 float32 BLOB（little-endian），寫入前 L2 正規化（T-02：舊資料量級不一致，
  短內容 norm≈25、長內容 norm≈1）；正規化後點積 = cosine
- 維度由呼叫端依設定傳入，不寫死；不符直接拋 `DimensionMismatch`
- 查詢先在 SQL 套 vault 過濾、再全量點積，不建 ANN，
  不會有「ANN 候選被範圍過濾掉而少回結果」的靜默漏失（A5）
- 資料庫中維度與設定不同的列（換模型後的殘留）不參與比對，
  由 doctor 的 `vector_dimension` 對帳項回報

本模組 import numpy；hook 路徑不得 import storage（doctor.hook_imports 有靜態檢查）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .db import transaction
from .errors import DimensionMismatch, NotFound
from .notes import note_seqs
from .timeutil import utc_now
from .vaults import resolve_read, resolve_write, vault_clause

_DTYPE = np.dtype("<f4")


def _validate_dim(dim: int) -> int:
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise DimensionMismatch(f"dim 必須是正整數，得到 {dim!r}")
    return dim


def normalize(vector: Sequence[float] | np.ndarray, dim: int) -> np.ndarray:
    """驗證維度與數值後做 L2 正規化，回傳 float32 一維陣列。"""
    _validate_dim(dim)
    arr = np.asarray(vector, dtype=np.float64)
    if arr.ndim != 1 or arr.shape[0] != dim:
        raise DimensionMismatch(f"向量維度 {arr.shape} 與設定 {dim} 不符")
    if not np.all(np.isfinite(arr)):
        raise DimensionMismatch("向量含 NaN 或無限值")
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        raise DimensionMismatch("零向量無法正規化")
    return (arr / norm).astype(_DTYPE)


def set_embedding(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    vector: Sequence[float] | np.ndarray,
    *,
    dim: int,
    model: str | None = None,
) -> None:
    """寫入／覆蓋一則 note 的 embedding（正規化後存）。"""
    normalized = normalize(vector, dim)
    with transaction(conn):
        key = resolve_write(conn, vault)
        seq = note_seqs(conn, key, [note_id]).get(note_id)
        if seq is None:
            raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
        conn.execute(
            """
            INSERT INTO note_embeddings (note_seq, dim, vector, model, updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (note_seq) DO UPDATE SET dim = excluded.dim,
                vector = excluded.vector, model = excluded.model,
                updated = excluded.updated
            """,
            (seq, dim, normalized.tobytes(), model, utc_now()),
        )


def get_embedding(
    conn: sqlite3.Connection, vault: str, note_id: str
) -> np.ndarray | None:
    scope = resolve_read(conn, vault)
    clause, params = vault_clause(scope, "n.vault")
    row = conn.execute(
        f"""
        SELECT e.vector FROM note_embeddings e JOIN notes n ON n.seq = e.note_seq
        WHERE n.id = ? AND {clause}
        """,
        (note_id, *params),
    ).fetchone()
    return None if row is None else np.frombuffer(row[0], dtype=_DTYPE)


@dataclass(frozen=True)
class VectorHit:
    note_id: str
    vault: str
    # cosine 相似度（-1–1）
    score: float


def search_vectors(
    conn: sqlite3.Connection,
    vault: str,
    query: Sequence[float] | np.ndarray,
    *,
    dim: int,
    limit: int = 20,
) -> list[VectorHit]:
    """在 vault 範圍內對全部向量做點積，回傳前 `limit` 名。"""
    scope = resolve_read(conn, vault)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    q = normalize(query, dim)
    clause, params = vault_clause(scope, "n.vault")
    rows = conn.execute(
        f"""
        SELECT n.id, n.vault, e.vector
        FROM note_embeddings e JOIN notes n ON n.seq = e.note_seq
        WHERE e.dim = ? AND length(e.vector) = ? AND {clause}
        """,
        (dim, dim * _DTYPE.itemsize, *params),
    ).fetchall()
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=_DTYPE).reshape(
        len(rows), dim
    )
    scores = matrix @ q
    k = min(limit, len(rows))
    top = np.argpartition(-scores, k - 1)[:k]
    # 同分依 note id 排序，結果穩定
    order = sorted(top.tolist(), key=lambda i: (-float(scores[i]), rows[i][0]))
    return [VectorHit(rows[i][0], rows[i][1], float(scores[i])) for i in order]
