"""管理用刪除（不經 HTTP／MCP）：刪單則 note、刪整個 vault。

每個刪除先「規劃」（只讀、列出會刪的筆數與 id），實際刪除在單一交易內重新規劃再刪，
並核對實際刪除筆數與規劃一致，不一致整段 rollback。

連帶資料：
- note：FTS 列（虛擬表無外鍵，手動刪）、向量與補算紀錄（外鍵 CASCADE）
- vault：上述 + 別名（CASCADE）+ episodes／concepts／injections
  （外鍵無 CASCADE，手動刪）
- 匯入對帳清單（`import_sources`／`import_vault_counts`）：刻意沒有外鍵
  （漏匯才看得出來），所以刪除時一併「退帳」——刪掉該 note 的清單列、
  把該 vault 的來源筆數減一；刪 vault 則清掉該 vault 的清單與筆數。
  刪除後對帳仍為綠，代價是重跑匯入會把刻意刪掉的 note 匯回來
  （清單本來就由匯入整批重寫）。沒做墓碑表：那需要 schema v4 並改對帳邏輯。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import transaction
from .errors import NotFound, StorageError, UnknownVault
from .vaults import resolve_write


class NeedsForce(StorageError):
    """vault 內還有資料，未加 force 不刪。"""


class PlanChanged(StorageError):
    """實際刪除筆數與規劃不一致（規劃後資料被改動）；已 rollback。"""


@dataclass(frozen=True)
class DeletePlan:
    """將刪除內容的 metadata（只有 id 與筆數，不含標題、內文）。"""

    target: str
    vault: str
    note_ids: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)
    # 需要 force 才能刪（vault 內仍有 note 或其他紀錄）
    requires_force: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "vault": self.vault,
            "counts": dict(self.counts),
            "note_ids": list(self.note_ids),
            "requires_force": self.requires_force,
        }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ── 單則 note ──


def plan_note_deletion(
    conn: sqlite3.Connection, vault: str, note_id: str
) -> DeletePlan:
    key = resolve_write(conn, vault)
    row = conn.execute(
        "SELECT seq FROM notes WHERE id = ? AND vault = ?", (note_id, key)
    ).fetchone()
    if row is None:
        raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
    seq = int(row["seq"])
    counts = {
        "notes": 1,
        "fts_rows": _count(
            conn, "SELECT count(*) FROM note_fts WHERE rowid = ?", (seq,)
        ),
        "embeddings": _count(
            conn, "SELECT count(*) FROM note_embeddings WHERE note_seq = ?", (seq,)
        ),
        "enrichment": _count(
            conn, "SELECT count(*) FROM note_enrichment WHERE note_seq = ?", (seq,)
        ),
        "import_sources": 0,
    }
    if _has_table(conn, "import_sources"):
        counts["import_sources"] = _count(
            conn, "SELECT count(*) FROM import_sources WHERE note_id = ?", (note_id,)
        )
    return DeletePlan(target="note", vault=key, note_ids=(note_id,), counts=counts)


def delete_note(conn: sqlite3.Connection, vault: str, note_id: str) -> DeletePlan:
    """刪一則 note 與其 FTS、向量、補算紀錄，並退掉匯入對帳清單（單一交易）。"""
    with transaction(conn):
        plan = plan_note_deletion(conn, vault, note_id)
        seq = conn.execute(
            "SELECT seq FROM notes WHERE id = ? AND vault = ?", (note_id, plan.vault)
        ).fetchone()["seq"]
        done = {"import_sources": 0}
        if plan.counts["import_sources"]:
            done["import_sources"] = _forget_import_notes(conn, [note_id])
        done["fts_rows"] = conn.execute(
            "DELETE FROM note_fts WHERE rowid = ?", (seq,)
        ).rowcount
        done["embeddings"] = plan.counts["embeddings"]
        done["enrichment"] = plan.counts["enrichment"]
        done["notes"] = conn.execute("DELETE FROM notes WHERE seq = ?", (seq,)).rowcount
        _check_cascade(conn)
        _verify(plan, done)
        return plan


def _forget_import_notes(conn: sqlite3.Connection, note_ids: list[str]) -> int:
    """刪掉 note 的清單列，並把對應 (source, vault) 的來源筆數各減一。"""
    removed = 0
    for note_id in note_ids:
        rows = conn.execute(
            "SELECT source, vault FROM import_sources WHERE note_id = ?", (note_id,)
        ).fetchall()
        for source, vault in rows:
            conn.execute(
                """
                UPDATE import_vault_counts SET source_count = source_count - 1
                WHERE source = ? AND vault = ? AND source_count > 0
                """,
                (source, vault),
            )
        removed += conn.execute(
            "DELETE FROM import_sources WHERE note_id = ?", (note_id,)
        ).rowcount
    return removed


# ── 整個 vault ──

# 參照 vaults(key) 但沒有 ON DELETE CASCADE 的表
_VAULT_RECORD_TABLES = ("episodes", "concepts", "injections")


def plan_vault_deletion(conn: sqlite3.Connection, key: str) -> DeletePlan:
    """只接受 vault 正式 key（不接受別名，避免誤刪）。"""
    row = conn.execute("SELECT key FROM vaults WHERE key = ?", (key,)).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (key,)
        ).fetchone()
        hint = f"（這是 {alias[0]!r} 的別名；請用正式 key）" if alias else ""
        raise UnknownVault(f"vault 不存在：{key!r}{hint}")
    note_ids = tuple(
        r[0]
        for r in conn.execute(
            "SELECT id FROM notes WHERE vault = ? ORDER BY seq", (key,)
        )
    )
    seqs = "SELECT seq FROM notes WHERE vault = ?"
    counts = {
        "notes": len(note_ids),
        "fts_rows": _count(
            conn, f"SELECT count(*) FROM note_fts WHERE rowid IN ({seqs})", (key,)
        ),
        "embeddings": _count(
            conn,
            f"SELECT count(*) FROM note_embeddings WHERE note_seq IN ({seqs})",
            (key,),
        ),
        "enrichment": _count(
            conn,
            f"SELECT count(*) FROM note_enrichment WHERE note_seq IN ({seqs})",
            (key,),
        ),
        "aliases": _count(
            conn, "SELECT count(*) FROM vault_aliases WHERE vault = ?", (key,)
        ),
        "import_sources": 0,
        "import_vault_counts": 0,
    }
    for table in _VAULT_RECORD_TABLES:
        counts[table] = _count(
            conn, f"SELECT count(*) FROM {table} WHERE vault = ?", (key,)
        )
    if _has_table(conn, "import_sources"):
        counts["import_sources"] = _count(
            conn,
            "SELECT count(*) FROM import_sources WHERE vault = ? "
            "OR note_id IN (SELECT id FROM notes WHERE vault = ?)",
            (key, key),
        )
        counts["import_vault_counts"] = _count(
            conn, "SELECT count(*) FROM import_vault_counts WHERE vault = ?", (key,)
        )
    requires_force = counts["notes"] > 0 or any(
        counts[t] > 0 for t in _VAULT_RECORD_TABLES
    )
    return DeletePlan(
        target="vault",
        vault=key,
        note_ids=note_ids,
        counts=counts,
        requires_force=requires_force,
    )


def delete_vault(
    conn: sqlite3.Connection, key: str, *, force: bool = False
) -> DeletePlan:
    """刪 vault 與其全部資料（單一交易）。vault 內有資料時必須 `force=True`。"""
    with transaction(conn):
        plan = plan_vault_deletion(conn, key)
        if plan.requires_force and not force:
            raise NeedsForce(
                f"vault {key!r} 內還有 {plan.counts['notes']} 則 note 或其他紀錄；"
                "確定要一併刪除請加 --force"
            )
        seqs = "SELECT seq FROM notes WHERE vault = ?"
        done: dict[str, int] = {"import_sources": 0, "import_vault_counts": 0}
        if _has_table(conn, "import_sources"):
            done["import_sources"] = conn.execute(
                "DELETE FROM import_sources WHERE vault = ? "
                "OR note_id IN (SELECT id FROM notes WHERE vault = ?)",
                (key, key),
            ).rowcount
            done["import_vault_counts"] = conn.execute(
                "DELETE FROM import_vault_counts WHERE vault = ?", (key,)
            ).rowcount
        done["fts_rows"] = conn.execute(
            f"DELETE FROM note_fts WHERE rowid IN ({seqs})", (key,)
        ).rowcount
        # 向量、補算紀錄隨 notes 以 CASCADE 刪除；先記下規劃值再核對剩餘
        done["embeddings"] = plan.counts["embeddings"]
        done["enrichment"] = plan.counts["enrichment"]
        done["notes"] = conn.execute(
            "DELETE FROM notes WHERE vault = ?", (key,)
        ).rowcount
        for table in _VAULT_RECORD_TABLES:
            done[table] = conn.execute(
                f"DELETE FROM {table} WHERE vault = ?", (key,)
            ).rowcount
        done["aliases"] = plan.counts["aliases"]
        if conn.execute("DELETE FROM vaults WHERE key = ?", (key,)).rowcount != 1:
            raise PlanChanged(f"vault {key!r} 刪除失敗")
        leftover = _count(
            conn, "SELECT count(*) FROM vault_aliases WHERE vault = ?", (key,)
        )
        if leftover:
            raise PlanChanged(f"vault {key!r} 仍有 {leftover} 個別名未刪")
        _check_cascade(conn)
        _verify(plan, done)
        return plan


def _check_cascade(conn: sqlite3.Connection) -> None:
    """向量與補算紀錄靠外鍵 CASCADE 刪除；外鍵沒開時這裡會抓到孤兒列並 rollback。"""
    for table in ("note_embeddings", "note_enrichment"):
        orphans = _count(
            conn,
            f"SELECT count(*) FROM {table} "
            "WHERE note_seq NOT IN (SELECT seq FROM notes)",
            (),
        )
        if orphans:
            raise PlanChanged(f"{table} 有 {orphans} 列孤兒資料（外鍵 CASCADE 未生效）")


def _verify(plan: DeletePlan, done: dict[str, int]) -> None:
    diff = {
        name: (expected, done.get(name))
        for name, expected in plan.counts.items()
        if done.get(name) != expected
    }
    if diff:
        raise PlanChanged(f"實際刪除筆數與規劃不符：{diff}")
