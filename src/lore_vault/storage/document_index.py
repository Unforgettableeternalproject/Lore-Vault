"""文件 chunk、FTS 索引、抽取／向量補算佇列與對帳（A19，T-63～T-68）。純標準庫。

- chunk 外部 id：`chunk:<document uuid>:<idx>`，由 (document_id, idx) 推導；
  不用 `document_chunks.seq`（INTEGER PRIMARY KEY 無 AUTOINCREMENT，刪最大列後會重用）。
- 索引只收「可索引」的文件（`documents.eligible_clause`：ready 且未被取代）：
  `sync_index` 依此補寫或移除 chunk_fts 列、移除向量列。版本切換、刪除都在同一交易內
  呼叫它；向量缺列由 worker 補算（佇列推導自「可索引文件的 chunk 缺 embedding」）。
- 抽取佇列 = `status='pending'`（嘗試／退避在 `document_enrichment` kind='extract'）；
  向量補算的嘗試以文件為單位記在 kind='embedding'。
- 抽取／向量寫回都先確認文件狀態仍符合（claim 過的 extracting、仍可索引），
  期間被刪或被取代就丟棄結果。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import fts
from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .documents import (
    DOCUMENT_ID_PREFIX,
    ERROR_CODES,
    STATUS_EXTRACTING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    eligible_clause,
)
from .timeutil import format_utc, parse_utc, utc_now

CHUNK_ID_PREFIX = "chunk:"
KIND_EXTRACT = "extract"
KIND_EMBEDDING = "embedding"
ENRICH_KINDS = (KIND_EXTRACT, KIND_EMBEDDING)
MAX_ERROR_CHARS = 300


# ── chunk id ────────────────────────────────────────────────────────


def chunk_id(document_id: str, idx: int) -> str:
    return f"{CHUNK_ID_PREFIX}{document_id[len(DOCUMENT_ID_PREFIX) :]}:{idx}"


def parse_chunk_id(value: str) -> tuple[str, int] | None:
    """`chunk:<uuid>:<idx>` → (document_id, idx)；格式不符回 None。"""
    if not isinstance(value, str) or not value.startswith(CHUNK_ID_PREFIX):
        return None
    body = value[len(CHUNK_ID_PREFIX) :]
    uid, sep, idx = body.rpartition(":")
    if not sep or not uid or not idx.isdigit():
        return None
    return f"{DOCUMENT_ID_PREFIX}{uid}", int(idx)


@dataclass(frozen=True)
class StoredChunk:
    seq: int
    document_id: str
    idx: int
    text: str
    locator: dict[str, Any]
    overlap: int

    @property
    def id(self) -> str:
        return chunk_id(self.document_id, self.idx)


def _row_to_chunk(row: sqlite3.Row) -> StoredChunk:
    return StoredChunk(
        seq=int(row["seq"]),
        document_id=row["document_id"],
        idx=int(row["idx"]),
        text=row["text"],
        locator=json.loads(row["locator"]),
        overlap=int(row["overlap"]),
    )


def chunks_of(conn: sqlite3.Connection, document_id: str) -> list[StoredChunk]:
    """某文件的全部 chunk（依 idx）。範圍檢查由呼叫端先做（document 已在範圍內）。"""
    return [
        _row_to_chunk(r)
        for r in conn.execute(
            "SELECT * FROM document_chunks WHERE document_id = ? ORDER BY idx",
            (document_id,),
        )
    ]


def chunks_by_key(
    conn: sqlite3.Connection, keys: Sequence[tuple[str, int]]
) -> dict[tuple[str, int], StoredChunk]:
    """(document_id, idx) → chunk。範圍檢查由呼叫端先做。"""
    found: dict[tuple[str, int], StoredChunk] = {}
    for document_id, idx in keys:
        row = conn.execute(
            "SELECT * FROM document_chunks WHERE document_id = ? AND idx = ?",
            (document_id, idx),
        ).fetchone()
        if row is not None:
            found[(document_id, idx)] = _row_to_chunk(row)
    return found


# ── 索引同步（呼叫端負責交易）─────────────────────────────────────────


def _is_eligible(conn: sqlite3.Connection, document_id: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM documents d WHERE d.id = ? AND {eligible_clause('d')}",
        (document_id,),
    ).fetchone()
    return row is not None


def sync_index(conn: sqlite3.Connection, document_id: str) -> None:
    """依文件目前是否可索引，補寫或移除它的 chunk_fts 列；不可索引時一併移除向量。"""
    if _is_eligible(conn, document_id):
        for row in conn.execute(
            """
            SELECT c.seq, c.text FROM document_chunks c
            WHERE c.document_id = ?
              AND NOT EXISTS (SELECT 1 FROM chunk_fts f WHERE f.rowid = c.seq)
            """,
            (document_id,),
        ).fetchall():
            conn.execute(
                "INSERT INTO chunk_fts (rowid, content) VALUES (?, ?)",
                (row[0], fts.expand(row[1])),
            )
        return
    seqs = "SELECT seq FROM document_chunks WHERE document_id = ?"
    conn.execute(f"DELETE FROM chunk_fts WHERE rowid IN ({seqs})", (document_id,))
    conn.execute(
        f"DELETE FROM document_chunk_embeddings WHERE chunk_seq IN ({seqs})",
        (document_id,),
    )


def version_chain(conn: sqlite3.Connection, document_id: str) -> list[str]:
    """文件本身與它 `supersedes` 一路往前的各版本 id（防環）。"""
    chain = [document_id]
    seen = {document_id}
    current = document_id
    while True:
        row = conn.execute(
            "SELECT supersedes FROM documents WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None or row[0] in seen:
            return chain
        current = row[0]
        seen.add(current)
        chain.append(current)


def sync_chain(conn: sqlite3.Connection, document_id: str) -> None:
    for doc in version_chain(conn, document_id):
        sync_index(conn, doc)


# ── 抽取佇列 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractCandidate:
    id: str
    vault: str
    filename: str
    mime: str
    sha256: str
    attempts: int


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """行程中斷遺留的 extracting 收回 pending（單一寫入程序、單一 worker 才安全）。"""
    with transaction(conn):
        return conn.execute(
            "UPDATE documents SET status = 'pending', updated = ? "
            "WHERE status = 'extracting'",
            (utc_now(),),
        ).rowcount


def extract_candidates(
    conn: sqlite3.Connection, *, now: str, limit: int
) -> list[ExtractCandidate]:
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    rows = conn.execute(
        """
        SELECT d.id, d.vault, d.filename, d.mime, d.sha256,
               coalesce(e.attempts, 0) AS attempts
        FROM documents d
        LEFT JOIN document_enrichment e
               ON e.document_id = d.id AND e.kind = 'extract'
        WHERE d.status = 'pending'
          AND (e.document_id IS NULL
               OR (e.status = 'pending' AND e.next_attempt <= ?))
        ORDER BY d.created, d.id
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()
    return [ExtractCandidate(*r) for r in rows]


def claim(conn: sqlite3.Connection, document_id: str) -> bool:
    """pending → extracting；別人先動過（被刪、已處理）回 False。"""
    with transaction(conn):
        return (
            conn.execute(
                "UPDATE documents SET status = 'extracting', updated = ? "
                "WHERE id = ? AND status = 'pending'",
                (utc_now(), document_id),
            ).rowcount
            == 1
        )


def finish_ready(
    conn: sqlite3.Connection,
    document_id: str,
    chunks: Iterable[Any],
    *,
    encoding: str | None,
    warnings: Sequence[Mapping[str, str]] = (),
) -> bool:
    """extracting → ready：寫入 chunk（idx、text、locator、overlap），同交易更新
    chunk_count、encoding、品質警示（`warnings`，空則 NULL）、補 FTS、並讓被它取代的
    舊版本退出索引。文件已不是 extracting（被刪除等）時不寫，回 False。"""
    warnings_json = (
        json.dumps([dict(w) for w in warnings], ensure_ascii=False)
        if warnings
        else None
    )
    items = list(chunks)
    if not items:
        raise ValueError(
            "ready 的文件至少要有一個 chunk（空結果應標 empty_extraction）"
        )
    with transaction(conn):
        row = conn.execute(
            "SELECT status FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None or row[0] != STATUS_EXTRACTING:
            return False
        # 防守：重跑時舊 chunk 的 FTS 列先清掉，不留孤兒（向量隨 chunk CASCADE）
        conn.execute(
            "DELETE FROM chunk_fts WHERE rowid IN "
            "(SELECT seq FROM document_chunks WHERE document_id = ?)",
            (document_id,),
        )
        conn.execute(
            "DELETE FROM document_chunks WHERE document_id = ?", (document_id,)
        )
        for chunk in items:
            conn.execute(
                """
                INSERT INTO document_chunks (document_id, idx, text, locator, overlap)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    chunk.idx,
                    chunk.text,
                    json.dumps(chunk.locator, ensure_ascii=False),
                    chunk.overlap,
                ),
            )
        conn.execute(
            """
            UPDATE documents SET status = 'ready', error_code = NULL,
                error_detail = NULL, chunk_count = ?, encoding = ?, warnings = ?,
                updated = ?
            WHERE id = ?
            """,
            (len(items), encoding, warnings_json, utc_now(), document_id),
        )
        conn.execute(
            "DELETE FROM document_enrichment WHERE document_id = ? AND kind = ?",
            (document_id, KIND_EXTRACT),
        )
        sync_chain(conn, document_id)
    return True


def finish_failed(
    conn: sqlite3.Connection, document_id: str, code: str, detail: str
) -> bool:
    """extracting／pending → failed（決定性失敗，不重試）。"""
    if code not in ERROR_CODES:
        raise ValueError(f"未知的錯誤碼：{code!r}")
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE documents SET status = 'failed', error_code = ?, error_detail = ?,
                chunk_count = 0, warnings = NULL, updated = ?
            WHERE id = ? AND status IN ('pending', 'extracting')
            """,
            (code, detail.strip()[:MAX_ERROR_CHARS] or code, utc_now(), document_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "DELETE FROM document_enrichment WHERE document_id = ? AND kind = ?",
            (document_id, KIND_EXTRACT),
        )
    return True


def _record_attempt(
    conn: sqlite3.Connection,
    document_id: str,
    kind: str,
    error: str,
    *,
    now: datetime,
    max_attempts: int,
    backoff_seconds: float,
) -> str:
    row = conn.execute(
        "SELECT attempts FROM document_enrichment WHERE document_id = ? AND kind = ?",
        (document_id, kind),
    ).fetchone()
    attempts = (int(row[0]) if row is not None else 0) + 1
    status = "failed" if attempts >= max_attempts else "pending"
    delay = backoff_seconds * (2 ** (attempts - 1))
    conn.execute(
        """
        INSERT INTO document_enrichment (document_id, kind, attempts, status,
                                         last_error, last_attempt, next_attempt)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (document_id, kind) DO UPDATE SET
            attempts = excluded.attempts, status = excluded.status,
            last_error = excluded.last_error, last_attempt = excluded.last_attempt,
            next_attempt = excluded.next_attempt
        """,
        (
            document_id,
            kind,
            attempts,
            status,
            error.strip()[:MAX_ERROR_CHARS] or "（無訊息）",
            format_utc(now),
            format_utc(now + timedelta(seconds=delay)),
        ),
    )
    return status


def record_extract_failure(
    conn: sqlite3.Connection,
    document_id: str,
    error: str,
    *,
    fail_code: str,
    now: datetime,
    max_attempts: int,
    backoff_seconds: float,
) -> str | None:
    """非預期的抽取失敗（可能是暫時性）：記一次嘗試；未達上限改回 pending 等退避，
    達上限標 failed（`fail_code`）。文件已不在 extracting 時回 None。"""
    if max_attempts <= 0:
        raise ValueError("max_attempts 必須大於 0")
    with transaction(conn):
        row = conn.execute(
            "SELECT status FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None or row[0] != STATUS_EXTRACTING:
            return None
        status = _record_attempt(
            conn,
            document_id,
            KIND_EXTRACT,
            error,
            now=now,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        if status == "failed":
            conn.execute(
                """
                UPDATE documents SET status = 'failed', error_code = ?,
                    error_detail = ?, updated = ?
                WHERE id = ?
                """,
                (fail_code, error.strip()[:MAX_ERROR_CHARS], utc_now(), document_id),
            )
        else:
            conn.execute(
                "UPDATE documents SET status = 'pending', updated = ? WHERE id = ?",
                (utc_now(), document_id),
            )
    return status


# ── chunk 向量補算佇列 ────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbedCandidate:
    seq: int
    document_id: str
    idx: int
    text: str


def embedding_candidates(
    conn: sqlite3.Connection, *, now: str, limit: int
) -> list[EmbedCandidate]:
    """可索引文件中缺向量的 chunk；該文件的向量補算已標 failed 或還在退避中的略過。"""
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    rows = conn.execute(
        f"""
        SELECT c.seq, c.document_id, c.idx, c.text
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN document_enrichment e
               ON e.document_id = d.id AND e.kind = 'embedding'
        WHERE {eligible_clause("d")}
          AND NOT EXISTS (SELECT 1 FROM document_chunk_embeddings v
                          WHERE v.chunk_seq = c.seq)
          AND (e.document_id IS NULL
               OR (e.status = 'pending' AND e.next_attempt <= ?))
        ORDER BY d.created, c.document_id, c.idx
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()
    return [EmbedCandidate(int(r[0]), r[1], int(r[2]), r[3]) for r in rows]


def chunk_is_indexable(conn: sqlite3.Connection, seq: int) -> bool:
    row = conn.execute(
        f"""
        SELECT 1 FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.seq = ? AND {eligible_clause("d")}
        """,
        (seq,),
    ).fetchone()
    return row is not None


def clear_embedding_attempts_if_done(
    conn: sqlite3.Connection, document_id: str
) -> None:
    """文件的 chunk 都有向量了才清嘗試紀錄（部分成功不歸零，重試才有上限）。"""
    missing = conn.execute(
        """
        SELECT 1 FROM document_chunks c WHERE c.document_id = ?
          AND NOT EXISTS (SELECT 1 FROM document_chunk_embeddings v
                          WHERE v.chunk_seq = c.seq)
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()
    if missing is None:
        conn.execute(
            "DELETE FROM document_enrichment WHERE document_id = ? AND kind = ?",
            (document_id, KIND_EMBEDDING),
        )


def record_embedding_failure(
    conn: sqlite3.Connection,
    document_id: str,
    error: str,
    *,
    now: datetime,
    max_attempts: int,
    backoff_seconds: float,
) -> str:
    """chunk 向量補算失敗：以文件為單位記一次嘗試；達上限標 failed（不再自動補）。"""
    if max_attempts <= 0:
        raise ValueError("max_attempts 必須大於 0")
    with transaction(conn):
        return _record_attempt(
            conn,
            document_id,
            KIND_EMBEDDING,
            error,
            now=now,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )


def reset_failed_embeddings(conn: sqlite3.Connection) -> int:
    """人工介入：清掉向量補算的失敗紀錄讓 worker 重試。"""
    with transaction(conn):
        return conn.execute(
            "DELETE FROM document_enrichment WHERE kind = 'embedding' "
            "AND status = 'failed'"
        ).rowcount


def pending_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(待抽取文件數, 可索引文件中缺向量的 chunk 數)，進度 log 用。"""
    docs = conn.execute(
        "SELECT count(*) FROM documents WHERE status IN ('pending', 'extracting')"
    ).fetchone()
    chunks = conn.execute(
        f"""
        SELECT count(*) FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE {eligible_clause("d")}
          AND NOT EXISTS (SELECT 1 FROM document_chunk_embeddings v
                          WHERE v.chunk_seq = c.seq)
        """
    ).fetchone()
    return int(docs[0]), int(chunks[0])


# ── 對帳（doctor 分類 documents）──────────────────────────────────────


def chunk_count_matches(conn: sqlite3.Connection) -> Reconciliation:
    """ready 文件的 `chunk_count` 等於實際 chunk 列數；非 ready 的文件不該有 chunk。"""
    rows = conn.execute(
        """
        SELECT d.id, d.status, d.chunk_count,
               (SELECT count(*) FROM document_chunks c WHERE c.document_id = d.id)
        FROM documents d ORDER BY d.id
        """
    ).fetchall()
    bad = [
        f"{r[0]}（{r[1]}）chunk_count={r[2]}，實際 {r[3]} 列"
        for r in rows
        if (r[1] == STATUS_READY and r[2] != r[3])
        or (r[1] != STATUS_READY and r[3] != 0)
    ]
    counts = {
        "ready": sum(1 for r in rows if r[1] == STATUS_READY),
        "mismatched": len(bad),
    }
    if not bad:
        return Reconciliation(
            "pass", f"{counts['ready']} 份 ready 文件筆數一致", counts
        )
    return Reconciliation(
        "fail",
        f"{len(bad)} 份文件的 chunk 數與記錄不符",
        counts,
        tuple(bad[:MAX_DETAILS]),
    )


def fts_rows_match_chunks(conn: sqlite3.Connection) -> Reconciliation:
    """chunk_fts 與「可索引文件的 chunk」一對一：缺列（搜不到）與多餘列都是 fail。"""
    indexable = f"""
        SELECT c.seq FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE {eligible_clause("d")}
    """
    missing = [
        r[0]
        for r in conn.execute(
            f"""
            SELECT seq FROM ({indexable})
            WHERE seq NOT IN (SELECT rowid FROM chunk_fts) ORDER BY seq
            """
        )
    ]
    extra = [
        r[0]
        for r in conn.execute(
            f"""
            SELECT rowid FROM chunk_fts WHERE rowid NOT IN ({indexable}) ORDER BY rowid
            """
        )
    ]
    total = int(conn.execute(f"SELECT count(*) FROM ({indexable})").fetchone()[0])
    rows = int(conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0])
    counts = {
        "indexable_chunks": total,
        "fts_rows": rows,
        "missing": len(missing),
        "extra": len(extra),
    }
    if not missing and not extra:
        return Reconciliation("pass", f"{total} 個可索引 chunk 皆有 FTS 列", counts)
    details = [f"缺 FTS 列：chunk seq {s}" for s in missing[:MAX_DETAILS]]
    details += [f"多餘 FTS 列：rowid {s}" for s in extra[:MAX_DETAILS]]
    return Reconciliation(
        "fail",
        f"chunk FTS 不一致（缺 {len(missing)}、多餘 {len(extra)}）",
        counts,
        tuple(details),
    )


def superseded_chunks_removed(conn: sqlite3.Connection) -> Reconciliation:
    """不可索引的文件（被取代、非 ready）不得留有 FTS 或向量列。"""
    rows = conn.execute(
        f"""
        SELECT d.id,
               (SELECT count(*) FROM chunk_fts f WHERE f.rowid IN
                   (SELECT seq FROM document_chunks WHERE document_id = d.id)),
               (SELECT count(*) FROM document_chunk_embeddings v WHERE v.chunk_seq IN
                   (SELECT seq FROM document_chunks WHERE document_id = d.id))
        FROM documents d WHERE NOT ({eligible_clause("d")})
        ORDER BY d.id
        """
    ).fetchall()
    bad = [f"{r[0]}：FTS {r[1]} 列、向量 {r[2]} 列" for r in rows if r[1] or r[2]]
    counts = {"non_indexable_documents": len(rows), "leaking": len(bad)}
    if not bad:
        return Reconciliation("pass", f"{len(rows)} 份不可索引文件皆已移出索引", counts)
    return Reconciliation(
        "fail",
        f"{len(bad)} 份被取代或非 ready 的文件仍在索引中（recall 會回舊版）",
        counts,
        tuple(bad[:MAX_DETAILS]),
    )


def vector_rows_match_chunks(
    conn: sqlite3.Connection, *, dim: int | None
) -> Reconciliation:
    """向量列只屬於可索引文件的 chunk、維度與設定一致（fail）；缺向量為 warn
    （背景補算中；積壓時間由 `documents.backlog` 判斷）。"""
    orphan = int(
        conn.execute(
            """
            SELECT count(*) FROM document_chunk_embeddings v
            WHERE v.chunk_seq NOT IN (SELECT seq FROM document_chunks)
            """
        ).fetchone()[0]
    )
    wrong_dim = (
        0
        if dim is None
        else int(
            conn.execute(
                "SELECT count(*) FROM document_chunk_embeddings WHERE dim != ?",
                (dim,),
            ).fetchone()[0]
        )
    )
    _, missing = pending_counts(conn)
    counts = {"orphans": orphan, "wrong_dim": wrong_dim, "missing": missing}
    if orphan or wrong_dim:
        return Reconciliation(
            "fail",
            f"chunk 向量不一致（孤兒 {orphan}、維度不符 {wrong_dim}）",
            counts,
        )
    if missing:
        return Reconciliation(
            "warn", f"{missing} 個可索引 chunk 缺向量（背景補算中）", counts
        )
    return Reconciliation("pass", "可索引 chunk 皆有向量", counts)


def stuck_processing(
    conn: sqlite3.Connection, *, now: datetime, max_age_seconds: float
) -> Reconciliation:
    """卡在 extracting 超過門檻：worker 死在抽取中途或沒在跑（fail）。"""
    rows = conn.execute(
        "SELECT id, vault, updated FROM documents WHERE status = ? ORDER BY updated",
        (STATUS_EXTRACTING,),
    ).fetchall()
    stuck = [
        (r[0], r[1], int((now - parse_utc(r[2])).total_seconds()))
        for r in rows
        if (now - parse_utc(r[2])).total_seconds() > max_age_seconds
    ]
    counts = {"extracting": len(rows), "stuck": len(stuck)}
    if not stuck:
        return Reconciliation("pass", f"{len(rows)} 份抽取中，無逾時", counts)
    return Reconciliation(
        "fail",
        f"{len(stuck)} 份文件卡在 extracting 超過 {int(max_age_seconds)} 秒",
        counts,
        tuple(f"{v}/{i}：{age} 秒" for i, v, age in stuck[:MAX_DETAILS]),
    )


def failed_documents(conn: sqlite3.Connection) -> Reconciliation:
    """抽取失敗與向量補算超過上限的文件數（warn：需要使用者處理，不會自癒）。"""
    failed = conn.execute(
        "SELECT vault, id, error_code FROM documents WHERE status = ? ORDER BY updated",
        (STATUS_FAILED,),
    ).fetchall()
    embed_failed = conn.execute(
        """
        SELECT d.vault, d.id, e.attempts, e.last_error FROM document_enrichment e
        JOIN documents d ON d.id = e.document_id
        WHERE e.kind = 'embedding' AND e.status = 'failed' ORDER BY d.id
        """
    ).fetchall()
    by_code: dict[str, int] = {}
    for row in failed:
        by_code[row[2]] = by_code.get(row[2], 0) + 1
    counts = {
        "failed": len(failed),
        "embedding_failed": len(embed_failed),
        **{f"failed_{code}": n for code, n in sorted(by_code.items())},
    }
    if not failed and not embed_failed:
        return Reconciliation("pass", "沒有失敗的文件", counts)
    details = [f"{r[0]}/{r[1]}：{r[2]}" for r in failed[:MAX_DETAILS]]
    details += [
        f"{r[0]}/{r[1]}：向量補算 {r[2]} 次失敗：{r[3]}"
        for r in embed_failed[:MAX_DETAILS]
    ]
    return Reconciliation(
        "warn",
        f"{len(failed)} 份抽取失敗、{len(embed_failed)} 份向量補算放棄",
        counts,
        tuple(details),
    )


def quality_warnings(conn: sqlite3.Connection) -> Reconciliation:
    """ready 文件的抽取品質警示（v10 `documents.warnings`，例如 cp950 判定信心低）。

    warn：已可搜尋，但內容可能解碼錯誤，需要使用者確認（轉成 UTF-8 重新上傳）。
    """
    rows = conn.execute(
        """
        SELECT vault, id, filename, warnings FROM documents
        WHERE status = ? AND warnings IS NOT NULL ORDER BY updated, id
        """,
        (STATUS_READY,),
    ).fetchall()
    by_code: dict[str, int] = {}
    details: list[str] = []
    for vault, doc_id, filename, raw in rows:
        items = [w for w in json.loads(raw) if isinstance(w, dict)]
        for item in items:
            code = str(item.get("code"))
            by_code[code] = by_code.get(code, 0) + 1
            if len(details) < MAX_DETAILS:
                details.append(f"{vault}/{doc_id}（{filename}）：{item.get('detail')}")
    counts = {
        "documents": len(rows),
        **{f"warning_{code}": n for code, n in sorted(by_code.items())},
    }
    if not rows:
        return Reconciliation("pass", "沒有帶品質警示的文件", counts)
    return Reconciliation(
        "warn",
        f"{len(rows)} 份 ready 文件帶抽取品質警示（內容可能解碼錯誤）",
        counts,
        tuple(details),
    )


def backlog(
    conn: sqlite3.Connection, *, now: datetime, max_age_seconds: float
) -> Reconciliation:
    """待抽取文件與缺向量 chunk；最舊一筆等超過門檻為 warn（worker 可能沒在跑）。"""
    docs, chunks = pending_counts(conn)
    oldest_doc = conn.execute(
        "SELECT min(updated) FROM documents WHERE status = ?", (STATUS_PENDING,)
    ).fetchone()[0]
    oldest_chunk = conn.execute(
        f"""
        SELECT min(d.updated) FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN document_enrichment e
               ON e.document_id = d.id AND e.kind = 'embedding'
        WHERE {eligible_clause("d")}
          AND NOT EXISTS (SELECT 1 FROM document_chunk_embeddings v
                          WHERE v.chunk_seq = c.seq)
          AND (e.document_id IS NULL OR e.status != 'failed')
        """
    ).fetchone()[0]
    stamps = [s for s in (oldest_doc, oldest_chunk) if s is not None]
    counts = {"documents_pending": docs, "chunks_missing_vectors": chunks}
    if not stamps:
        counts["oldest_age_seconds"] = 0
        return Reconciliation("pass", "沒有待處理的文件", counts)
    age = max(0, int((now - parse_utc(min(stamps))).total_seconds()))
    counts["oldest_age_seconds"] = age
    summary = f"待抽取 {docs} 份、缺向量 {chunks} 個 chunk，最舊等 {age} 秒"
    if age > max_age_seconds:
        return Reconciliation(
            "warn",
            summary + f"（上限 {int(max_age_seconds)}），worker 可能沒在執行",
            counts,
        )
    return Reconciliation("pass", summary, counts)
