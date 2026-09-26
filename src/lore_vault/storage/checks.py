"""儲存層對帳（純函式、純標準庫）：輸入連線，回傳結構化結果。

不 import numpy，也不依賴 doctor；doctor 在 `builtin.py` 把結果轉成 CheckResult。
每一項都有「破壞後會紅」的測試（tests/test_storage_checks.py）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from lore_vault.schema.chars import FORBIDDEN_CONTROL, has_invalid

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


# ── 控制字元（doctor storage.control_chars）──────────────────────────
#
# 依據（tests/storage/test_control_chars.py 實測，SQLite 3.50）：Python sqlite3 寫入
# 含 NUL 的 TEXT 後能完整讀回，FTS5 也照樣索引 NUL 兩側的 token；但 SQL 端
# `length()` 只算到 NUL 為止、`LIKE` 與 `substr()` 在 NUL 處截斷、JSON1 把含原始 NUL
# 的字串判為不合法（SQLite 文件也說明含 NUL 的 TEXT 在多數函式上行為未定義）。
# 所以掃描不能靠文字函式，一律 `CAST(col AS BLOB)` 後找位元組：UTF-8 中 0x00–0x1F
# 只會以單一位元組出現（多位元組序列的每個位元組都 ≥ 0x80），逐位元組比對是精確的。
#
# JSON 欄（notes.topics、concepts.data、episodes.data）由 `json.dumps` 寫入，控制字元
# 會被跳脫成 `\u00XX`、`\b`、`\f`，孤立 surrogate 是 `\udXXX`，原始位元組不會出現。
# SQL 先以這些子字串粗篩，再在 Python 解析 JSON 逐字串確認（排除 `\\u0041` 這類
# 字面反斜線的誤判）；JSON 解析失敗（例如被直接以 SQL 塞進原始控制字元）也算問題。

_FORBIDDEN_BYTES = tuple(sorted(ord(ch) for ch in FORBIDDEN_CONTROL))
_JSON_ESCAPE_HINTS = ("\\u00", "\\b", "\\f", "\\ud", "\\uD")


def _raw_clause(column: str) -> str:
    blob = f"CAST({column} AS BLOB)"
    return " OR ".join(f"instr({blob}, X'{b:02X}') > 0" for b in _FORBIDDEN_BYTES)


def _json_clause(column: str) -> tuple[str, list[str]]:
    hints = " OR ".join(f"instr({column}, ?) > 0" for _ in _JSON_ESCAPE_HINTS)
    return f"{_raw_clause(column)} OR {hints}", list(_JSON_ESCAPE_HINTS)


def _json_has_invalid(text: str | None) -> bool:
    if text is None:
        return False
    try:
        value = json.loads(text)
    except ValueError:
        # 只有在粗篩命中時才會解析到這裡；解析失敗代表 JSON 內有原始控制字元
        return True
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if has_invalid(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def control_chars(conn: sqlite3.Connection) -> Reconciliation:
    """notes（title／body／summary／topics）、concepts、episodes 的 data 中是否有
    禁用的控制字元（tab、LF、CR 以外的 C0）或孤立 surrogate。有即 fail 並列 id。

    寫入路徑會拒收、匯入與 episode 收料會清理，所以正常情況永遠是 0；
    出現代表有路徑繞過了防線（直接改 DB、舊版程式等）。
    """
    found: list[str] = []
    per_table = {"notes": 0, "concepts": 0, "episodes": 0}

    raw_cols = ("title", "body", "summary")
    topics_clause, topics_args = _json_clause("topics")
    note_where = " OR ".join(
        [*(f"({_raw_clause(c)})" for c in raw_cols), f"({topics_clause})"]
    )
    for note_id, *values in conn.execute(
        f"SELECT id, title, body, summary, topics FROM notes WHERE {note_where} "
        "ORDER BY seq",
        topics_args,
    ):
        raw = dict(zip(raw_cols, values[:3], strict=True))
        fields = [c for c, v in raw.items() if v is not None and has_invalid(v)]
        if _json_has_invalid(values[3]):
            fields.append("topics")
        if fields:
            per_table["notes"] += 1
            found.append(f"note {note_id}：{'、'.join(fields)}")

    data_clause, data_args = _json_clause("data")
    for concept_id, data in conn.execute(
        f"SELECT id, data FROM concepts WHERE {data_clause} ORDER BY id", data_args
    ):
        if _json_has_invalid(data):
            per_table["concepts"] += 1
            found.append(f"concept {concept_id}：data")

    for session_id, prompt_id, turn_index, data in conn.execute(
        "SELECT session_id, prompt_id, turn_index, data FROM episodes "
        f"WHERE {data_clause} ORDER BY seq",
        data_args,
    ):
        if _json_has_invalid(data):
            per_table["episodes"] += 1
            found.append(f"episode {session_id}/{prompt_id}/{turn_index}：data")

    counts = {**per_table, "total": len(found)}
    if not found:
        return Reconciliation("pass", "沒有禁用的控制字元", counts)
    return Reconciliation(
        "fail",
        f"{len(found)} 筆資料含禁用的控制字元（note {per_table['notes']}、"
        f"concept {per_table['concepts']}、episode {per_table['episodes']}）",
        counts,
        tuple(found[:MAX_DETAILS]),
    )
