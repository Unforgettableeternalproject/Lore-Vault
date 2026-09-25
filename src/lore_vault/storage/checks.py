"""儲存層對帳（純函式、純標準庫）：輸入連線，回傳結構化結果。

不 import numpy，也不依賴 doctor；doctor 在 `builtin.py` 把結果轉成 CheckResult。
每一項都有「破壞後會紅」的測試（tests/test_storage_checks.py）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .migrate import SCHEMA_VERSION, current_version

# details 最多列幾筆，避免大量不一致時灌爆報告
MAX_DETAILS = 20


@dataclass(frozen=True)
class Reconciliation:
    """`status`：'pass'／'warn'／'fail'（對應 doctor 的 CheckResult）。"""

    status: str
    summary: str
    counts: dict[str, int] = field(default_factory=dict)
    details: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def schema_version(
    conn: sqlite3.Connection, *, expected: int = SCHEMA_VERSION
) -> Reconciliation:
    """資料庫 `user_version` 與程式預期版本是否一致。"""
    actual = current_version(conn)
    counts = {"db_version": actual, "expected_version": expected}
    if actual == expected:
        return Reconciliation("pass", f"schema 版本 {actual}", counts)
    direction = "比程式新" if actual > expected else "尚未遷移到最新"
    return Reconciliation(
        "fail",
        f"schema 版本 {actual} 與程式預期 {expected} 不符（{direction}）",
        counts,
    )


def fts_rows(conn: sqlite3.Connection) -> Reconciliation:
    """FTS 索引列與 notes 一對一：列數相同，且沒有缺列／孤兒列。"""
    notes = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
    rows = int(conn.execute("SELECT count(*) FROM note_fts").fetchone()[0])
    missing = [
        r[0]
        for r in conn.execute(
            """
            SELECT id FROM notes WHERE seq NOT IN (SELECT rowid FROM note_fts)
            ORDER BY seq
            """
        )
    ]
    orphans = [
        r[0]
        for r in conn.execute(
            """
            SELECT rowid FROM note_fts WHERE rowid NOT IN (SELECT seq FROM notes)
            ORDER BY rowid
            """
        )
    ]
    counts = {
        "notes": notes,
        "fts_rows": rows,
        "missing": len(missing),
        "orphans": len(orphans),
    }
    if notes == rows and not missing and not orphans:
        return Reconciliation("pass", f"{notes} 則 note 皆有索引", counts)
    details = [f"缺 FTS 列：note {nid}" for nid in missing[:MAX_DETAILS]]
    details += [f"孤兒 FTS 列：rowid {rid}" for rid in orphans[:MAX_DETAILS]]
    return Reconciliation(
        "fail",
        f"FTS 列數 {rows} 與 note 數 {notes} 不一致"
        f"（缺 {len(missing)}、孤兒 {len(orphans)}）",
        counts,
        tuple(details),
    )


def missing_embeddings(conn: sqlite3.Connection) -> Reconciliation:
    """沒有 embedding 的 note 數（逐 vault）。

    `write` 不等 embedding、背景補算，所以非零是 warn 而非 fail。
    """
    rows = conn.execute(
        """
        SELECT n.vault, count(*) AS missing
        FROM notes n LEFT JOIN note_embeddings e ON e.note_seq = n.seq
        WHERE e.note_seq IS NULL
        GROUP BY n.vault ORDER BY n.vault
        """
    ).fetchall()
    total = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
    missing = sum(int(r[1]) for r in rows)
    counts = {"notes": total, "missing": missing}
    if missing == 0:
        return Reconciliation("pass", f"{total} 則 note 皆有 embedding", counts)
    return Reconciliation(
        "warn",
        f"{missing} 則 note 缺 embedding（背景補算前 recall 只走 lexical）",
        counts,
        tuple(f"{r[0]}: {r[1]}" for r in rows[:MAX_DETAILS]),
    )


def missing_summaries(conn: sqlite3.Connection) -> Reconciliation:
    """沒有 summary 的 note 數（逐 vault）。

    摘要由背景佇列非同步補（A14／D4），期間 recall 以正文首段頂替並標
    `summary_source: "lead"`，所以非零是 warn 而非 fail。
    """
    rows = conn.execute(
        """
        SELECT vault, count(*) AS missing FROM notes WHERE summary IS NULL
        GROUP BY vault ORDER BY vault
        """
    ).fetchall()
    total = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
    missing = sum(int(r[1]) for r in rows)
    counts = {"notes": total, "missing": missing}
    if missing == 0:
        return Reconciliation("pass", f"{total} 則 note 皆有 summary", counts)
    return Reconciliation(
        "warn",
        f"{missing} 則 note 缺 summary（補齊前 recall 以正文首段頂替）",
        counts,
        tuple(f"{r[0]}: {r[1]}" for r in rows[:MAX_DETAILS]),
    )


def vector_dimension(conn: sqlite3.Connection, *, dim: int) -> Reconciliation:
    """維度與設定不符（或 BLOB 長度與宣告維度不符）的向量數。

    這些向量不參與比對，等同靜默缺向量，所以是 fail。
    """
    rows = conn.execute(
        """
        SELECT n.id, e.dim, length(e.vector) AS bytes
        FROM note_embeddings e JOIN notes n ON n.seq = e.note_seq
        WHERE e.dim != ? OR length(e.vector) != ?
        ORDER BY n.seq
        """,
        (dim, dim * 4),
    ).fetchall()
    total = int(conn.execute("SELECT count(*) FROM note_embeddings").fetchone()[0])
    counts = {"vectors": total, "mismatched": len(rows), "expected_dim": dim}
    if not rows:
        return Reconciliation("pass", f"{total} 條向量皆為 {dim} 維", counts)
    return Reconciliation(
        "fail",
        f"{len(rows)} 條向量維度與設定 {dim} 不符",
        counts,
        tuple(f"note {r[0]}: dim={r[1]} bytes={r[2]}" for r in rows[:MAX_DETAILS]),
    )
