"""背景補算（summary／embedding）的儲存原語與對帳（T-19、D4）。純標準庫。

- 佇列是推導的：缺 summary = `notes.summary IS NULL`；缺 embedding = 沒有
  `note_embeddings` 列。`note_enrichment` 只記嘗試次數與失敗狀態。
- 補算結果寫回時比對 `notes.updated`：補算期間 note 被更新（race）就丟棄結果，
  不覆蓋新版本。
- 寫回 summary **不推進 `updated`**：summary 是衍生資料，`updated` 是樂觀鎖版本
  與列表排序鍵，背景寫入推版本會讓 agent 手上的 `expected_updated` 無故失效。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from lore_vault.schema.chars import sanitize_text

from . import fts
from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .timeutil import format_utc, parse_utc

KINDS = ("summary", "embedding")
# last_error 最多存幾個字
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class Candidate:
    """一則待補算的 note（讀取當下的版本）。"""

    seq: int
    note_id: str
    vault: str
    title: str
    body: str
    updated: str
    # 針對目前版本已失敗幾次
    attempts: int
    # note 所屬 vault 的 space（寫回 embedding 時的範圍參數）
    space: str


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知的補算種類：{kind!r}")


def _missing_clause(kind: str) -> str:
    if kind == "summary":
        return "n.summary IS NULL"
    return "NOT EXISTS (SELECT 1 FROM note_embeddings v WHERE v.note_seq = n.seq)"


def candidates(
    conn: sqlite3.Connection, kind: str, *, now: str, limit: int
) -> list[Candidate]:
    """缺 `kind` 且可以嘗試的 note：沒有嘗試紀錄、紀錄屬於舊版本、
    或仍為 pending 且已過 `next_attempt`。已標記失敗（目前版本）的不回傳。"""
    _check_kind(kind)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    rows = conn.execute(
        f"""
        SELECT n.seq, n.id, n.vault, n.title, n.body, n.updated,
               CASE WHEN e.for_updated = n.updated THEN e.attempts ELSE 0 END
                   AS attempts,
               vt.space
        FROM notes n
        JOIN vaults vt ON vt.key = n.vault
        LEFT JOIN note_enrichment e ON e.note_seq = n.seq AND e.kind = ?
        WHERE {_missing_clause(kind)}
          AND (e.note_seq IS NULL OR e.for_updated != n.updated
               OR (e.status = 'pending' AND e.next_attempt <= ?))
        ORDER BY n.seq
        LIMIT ?
        """,
        (kind, now, limit),
    ).fetchall()
    return [
        Candidate(r[0], r[1], r[2], r[3], r[4], r[5], int(r[6]), r[7]) for r in rows
    ]


def _current_row(conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT seq, title, summary, body, topics, updated FROM notes WHERE seq = ?",
        (seq,),
    ).fetchone()


def write_summary_if_current(
    conn: sqlite3.Connection, seq: int, expected_updated: str, summary: str
) -> bool:
    """note 版本仍是 `expected_updated` 且仍缺 summary 時寫入（同交易更新 FTS、
    清掉嘗試紀錄）；否則不動並回傳 False。不推進 `updated`。

    摘要是 LLM 產物（衍生文字）：夾帶的控制字元清理成可見形式而不是拒收——
    拒收只會讓補算無限重試，doctor `storage.control_chars` 也永遠紅。"""
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 不可為空")
    summary, _ = sanitize_text(summary)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE notes SET summary = ?
            WHERE seq = ? AND updated = ? AND summary IS NULL
            """,
            (summary, seq, expected_updated),
        )
        if cursor.rowcount != 1:
            return False
        row = _current_row(conn, seq)
        assert row is not None
        fts.upsert_row(
            conn,
            seq,
            row["title"],
            row["summary"],
            row["body"],
            json.loads(row["topics"]),
        )
        clear(conn, seq, "summary")
    return True


def note_is_current(conn: sqlite3.Connection, seq: int, expected_updated: str) -> bool:
    row = conn.execute("SELECT updated FROM notes WHERE seq = ?", (seq,)).fetchone()
    return row is not None and row[0] == expected_updated


def clear(conn: sqlite3.Connection, seq: int, kind: str) -> None:
    """補算成功：刪掉嘗試紀錄。"""
    _check_kind(kind)
    conn.execute(
        "DELETE FROM note_enrichment WHERE note_seq = ? AND kind = ?", (seq, kind)
    )


def record_failure(
    conn: sqlite3.Connection,
    seq: int,
    kind: str,
    for_updated: str,
    error: str,
    *,
    now: datetime,
    max_attempts: int,
    backoff_seconds: float,
) -> str | None:
    """記一次失敗；次數到 `max_attempts` 標記 failed。

    回傳寫入後的狀態（'pending'／'failed'）；note 已被刪除或已更新成新版本時
    不記錄（舊版本的失敗不算在新版本頭上），回傳 None。
    """
    _check_kind(kind)
    if max_attempts <= 0:
        raise ValueError("max_attempts 必須大於 0")
    stamp = format_utc(now)
    message = error.strip()[:MAX_ERROR_CHARS] or "（無訊息）"
    with transaction(conn):
        if not note_is_current(conn, seq, for_updated):
            return None
        row = conn.execute(
            """
            SELECT attempts, for_updated FROM note_enrichment
            WHERE note_seq = ? AND kind = ?
            """,
            (seq, kind),
        ).fetchone()
        previous = row[0] if row is not None and row[1] == for_updated else 0
        attempts = previous + 1
        status = "failed" if attempts >= max_attempts else "pending"
        delay = backoff_seconds * (2 ** (attempts - 1))
        next_attempt = format_utc(now + timedelta(seconds=delay))
        conn.execute(
            """
            INSERT INTO note_enrichment (note_seq, kind, for_updated, attempts, status,
                                         last_error, last_attempt, next_attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (note_seq, kind) DO UPDATE SET
                for_updated = excluded.for_updated, attempts = excluded.attempts,
                status = excluded.status, last_error = excluded.last_error,
                last_attempt = excluded.last_attempt,
                next_attempt = excluded.next_attempt
            """,
            (seq, kind, for_updated, attempts, status, message, stamp, next_attempt),
        )
    return status


def reset_failed(conn: sqlite3.Connection, kind: str | None = None) -> int:
    """人工介入：清掉失敗紀錄讓 worker 重新嘗試。回傳清掉的筆數。"""
    if kind is not None:
        _check_kind(kind)
    with transaction(conn):
        if kind is None:
            cursor = conn.execute("DELETE FROM note_enrichment WHERE status = 'failed'")
        else:
            cursor = conn.execute(
                "DELETE FROM note_enrichment WHERE status = 'failed' AND kind = ?",
                (kind,),
            )
    return cursor.rowcount


# ── 對帳（doctor 在 builtin.py 轉成 CheckResult）────────────────────────


def _has_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("note_enrichment",),
        ).fetchone()
        is not None
    )


class MissingEnrichmentTable(LookupError):
    """資料庫尚未遷移到含 `note_enrichment` 的版本。"""


def failed_enrichments(conn: sqlite3.Connection) -> Reconciliation:
    """超過重試上限、目前版本仍缺結果的補算數。

    非零為 fail：這些 note 不會再被自動補算（不自癒），沒人介入就永遠缺摘要／
    向量；recall 雖能以首段頂替或退回 lexical，但品質已靜默下降，必須讓 doctor
    非零退出才會有人處理（修正後用 `--reset-failed` 重跑）。
    """
    if not _has_table(conn):
        raise MissingEnrichmentTable("缺少 note_enrichment 表（schema 未遷移）")
    rows = conn.execute(
        f"""
        SELECT e.kind, n.vault, n.id, e.attempts, e.last_error
        FROM note_enrichment e JOIN notes n ON n.seq = e.note_seq
        WHERE e.status = 'failed' AND e.for_updated = n.updated
          AND ((e.kind = 'summary' AND {_missing_clause("summary")})
               OR (e.kind = 'embedding' AND {_missing_clause("embedding")}))
        ORDER BY e.kind, n.seq
        """
    ).fetchall()
    counts = {
        "summary_failed": sum(1 for r in rows if r[0] == "summary"),
        "embedding_failed": sum(1 for r in rows if r[0] == "embedding"),
    }
    if not rows:
        return Reconciliation("pass", "沒有補算失敗的 note", counts)
    return Reconciliation(
        "fail",
        f"{len(rows)} 項補算超過重試上限（summary {counts['summary_failed']}、"
        f"embedding {counts['embedding_failed']}）",
        counts,
        tuple(f"{r[0]} {r[1]}/{r[2]}（{r[3]} 次）：{r[4]}" for r in rows[:MAX_DETAILS]),
    )


def enrichment_backlog(
    conn: sqlite3.Connection, *, now: datetime, max_age_seconds: float
) -> Reconciliation:
    """尚待補算（未標記失敗）的項目數與最舊等待時間。

    有積壓本身是正常的（write 不等 LLM）；最舊一筆等超過 `max_age_seconds`
    代表 worker 沒在跑或跟不上，記 warn。以 note 的 `updated` 當入列時間。
    """
    if not _has_table(conn):
        raise MissingEnrichmentTable("缺少 note_enrichment 表（schema 未遷移）")
    counts: dict[str, int] = {}
    oldest: str | None = None
    for kind in KINDS:
        row = conn.execute(
            f"""
            SELECT count(*), min(n.updated) FROM notes n
            LEFT JOIN note_enrichment e ON e.note_seq = n.seq AND e.kind = ?
            WHERE {_missing_clause(kind)}
              AND NOT (e.note_seq IS NOT NULL AND e.status = 'failed'
                       AND e.for_updated = n.updated)
            """,
            (kind,),
        ).fetchone()
        counts[f"{kind}_pending"] = int(row[0])
        if row[1] is not None and (oldest is None or row[1] < oldest):
            oldest = row[1]
    if oldest is None:
        counts["oldest_age_seconds"] = 0
        return Reconciliation("pass", "沒有待補算的項目", counts)
    age = max(0, int((now - parse_utc(oldest)).total_seconds()))
    counts["oldest_age_seconds"] = age
    total = counts["summary_pending"] + counts["embedding_pending"]
    if age > max_age_seconds:
        return Reconciliation(
            "warn",
            f"{total} 項待補算，最舊已等 {age} 秒（上限 {int(max_age_seconds)}），"
            "worker 可能沒在執行",
            counts,
        )
    return Reconciliation("pass", f"{total} 項待補算，最舊等 {age} 秒", counts)
