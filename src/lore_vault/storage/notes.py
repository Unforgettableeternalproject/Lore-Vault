"""Note 的儲存原語。主表、FTS、向量的變動在同一個交易內完成。

時間戳在寫入時正規化成 `YYYY-MM-DDTHH:MM:SS.sssZ`（T-23）；
`updated` 是樂觀鎖版本，`update_note_if` 以資料庫中的字串精確比對。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from lore_vault.schema import Note, SchemaError

from . import fts
from .db import transaction
from .errors import DuplicateRecord, NotFound, VaultRequired
from .timeutil import next_after, normalize_utc
from .vaults import resolve_read, resolve_write, vault_clause

# update_note_if 允許改動的欄位
UPDATABLE_FIELDS = frozenset(
    {"title", "summary", "body", "topics", "links", "supersedes"}
)
# 這些欄位一變，舊 embedding 就不再代表內容，同一交易內刪掉
# （doctor 會回報缺向量、背景補算）
_EMBEDDING_FIELDS = frozenset({"title", "body"})


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        vault=row["vault"],
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        topics=json.loads(row["topics"]),
        links=json.loads(row["links"]),
        supersedes=row["supersedes"],
        created=row["created"],
        updated=row["updated"],
    )


def _check_vault_matches(conn: sqlite3.Connection, vault_key: str, note: Note) -> None:
    if resolve_write(conn, note.vault) != vault_key:
        raise VaultRequired(
            f"note.vault {note.vault!r} 與指定的 vault {vault_key!r} 不一致"
        )


def insert_note(conn: sqlite3.Connection, vault: str, note: Note) -> Note:
    """新增 note；回傳實際存下的版本（vault 為現行 key、時間戳已正規化）。

    `vault` 參數必填且必須與 `note.vault` 指向同一個 vault（別名會解析成現行 key）。
    """
    with transaction(conn):
        key = resolve_write(conn, vault)
        _check_vault_matches(conn, key, note)
        stored = dataclasses.replace(
            note,
            vault=key,
            created=normalize_utc(note.created),
            updated=normalize_utc(note.updated),
        )
        # 在同一個 BEGIN IMMEDIATE 交易內先查存在（已持有寫鎖，無競態），
        # 不靠 IntegrityError 的訊息字串判斷是哪個約束
        if conn.execute("SELECT 1 FROM notes WHERE id = ?", (stored.id,)).fetchone():
            raise DuplicateRecord(f"note id 已存在：{note.id!r}")
        cursor = conn.execute(
            """
            INSERT INTO notes (id, vault, title, summary, body, topics, links,
                               supersedes, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.id,
                key,
                stored.title,
                stored.summary,
                stored.body,
                json.dumps(list(stored.topics), ensure_ascii=False),
                json.dumps(list(stored.links), ensure_ascii=False),
                stored.supersedes,
                stored.created,
                stored.updated,
            ),
        )
        seq = cursor.lastrowid
        assert seq is not None
        fts.upsert_row(
            conn, seq, stored.title, stored.summary, stored.body, stored.topics
        )
    return stored


def get_notes(conn: sqlite3.Connection, vault: str, ids: Sequence[str]) -> list[Note]:
    """依 id 取 note，保持傳入順序；不在範圍內的 id 不回傳（呼叫端可比對缺漏）。"""
    scope = resolve_read(conn, vault)
    if isinstance(ids, str):
        raise TypeError("ids 必須是清單，不可傳單一字串")
    if not ids:
        return []
    clause, params = vault_clause(scope, "vault")
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM notes WHERE id IN ({placeholders}) AND {clause}",
        (*ids, *params),
    ).fetchall()
    by_id = {row["id"]: _row_to_note(row) for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def get_note(conn: sqlite3.Connection, vault: str, note_id: str) -> Note:
    found = get_notes(conn, vault, [note_id])
    if not found:
        raise NotFound(f"vault {vault!r} 內找不到 note {note_id!r}")
    return found[0]


def list_notes(
    conn: sqlite3.Connection,
    vault: str,
    *,
    since: str | None = None,
    topics: Sequence[str] | None = None,
    limit: int = 50,
    cursor: tuple[str, str] | None = None,
) -> tuple[list[Note], tuple[str, str] | None]:
    """依 (updated, id) 由新到舊分頁。回傳 (本頁, 下一頁 cursor 或 None)。

    `since`：只列 updated >= since 的 note（ISO-8601 UTC）。
    `topics`：只列至少含其中一個 topic 的 note（大小寫精確比對）；在 SQL 內、
    LIMIT 之前過濾，分頁不會因為事後過濾而少回。
    """
    scope = resolve_read(conn, vault)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if since is not None:
        conditions.append("updated >= ?")
        args.append(normalize_utc(since))
    if topics is not None:
        if isinstance(topics, str):
            raise TypeError("topics 必須是清單，不可傳單一字串")
        if not topics:
            raise ValueError("topics 不可為空清單；不過濾請傳 None")
        placeholders = ",".join("?" * len(topics))
        conditions.append(
            f"EXISTS (SELECT 1 FROM json_each(notes.topics) WHERE value IN "
            f"({placeholders}))"
        )
        args.extend(topics)
    if cursor is not None:
        conditions.append("(updated, id) < (?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT * FROM notes WHERE {" AND ".join(conditions)}
        ORDER BY updated DESC, id DESC LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    notes = [_row_to_note(row) for row in rows[:limit]]
    next_cursor = (notes[-1].updated, notes[-1].id) if len(rows) > limit else None
    return notes, next_cursor


def count_notes(conn: sqlite3.Connection, vault: str) -> int:
    scope = resolve_read(conn, vault)
    clause, params = vault_clause(scope, "vault")
    return int(
        conn.execute(f"SELECT count(*) FROM notes WHERE {clause}", params).fetchone()[0]
    )


def update_note_if(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    expected_updated: str,
    changes: dict[str, Any],
    *,
    now: str | None = None,
) -> Note | None:
    """條件更新原語（樂觀鎖）：只有資料庫中的 `updated` 字串與
    `expected_updated` 完全相同才寫入。

    - 成功：回傳新版本 note（`updated` 嚴格晚於舊版本）
    - 版本不符：回傳 None，不寫入任何東西（衝突錯誤由上層 T-21 決定怎麼回）
    - note 不在該 vault：拋 `NotFound`
    title／body 變動時同一交易內刪除舊 embedding。
    """
    unknown = sorted(set(changes) - UPDATABLE_FIELDS)
    if unknown:
        raise SchemaError(f"update_note_if 不可更新欄位 {unknown}")
    with transaction(conn):
        key = resolve_write(conn, vault)
        row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND vault = ?", (note_id, key)
        ).fetchone()
        if row is None:
            raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
        if row["updated"] != expected_updated:
            return None
        current = _row_to_note(row)
        # 透過 dataclass 重新驗證（空標題、非字串 topics 等在這裡擋下）
        updated = dataclasses.replace(
            current, **changes, updated=next_after(current.updated, now)
        )
        cursor = conn.execute(
            """
            UPDATE notes SET title = ?, summary = ?, body = ?, topics = ?, links = ?,
                             supersedes = ?, updated = ?
            WHERE seq = ? AND updated = ?
            """,
            (
                updated.title,
                updated.summary,
                updated.body,
                json.dumps(list(updated.topics), ensure_ascii=False),
                json.dumps(list(updated.links), ensure_ascii=False),
                updated.supersedes,
                updated.updated,
                row["seq"],
                expected_updated,
            ),
        )
        if cursor.rowcount != 1:
            return None
        fts.upsert_row(
            conn,
            row["seq"],
            updated.title,
            updated.summary,
            updated.body,
            updated.topics,
        )
        if _EMBEDDING_FIELDS & set(changes) and (
            updated.title != current.title or updated.body != current.body
        ):
            conn.execute(
                "DELETE FROM note_embeddings WHERE note_seq = ?", (row["seq"],)
            )
    return updated


def delete_note(conn: sqlite3.Connection, vault: str, note_id: str) -> None:
    """刪除 note 與其 FTS 列、embedding（同一交易）。"""
    with transaction(conn):
        key = resolve_write(conn, vault)
        row = conn.execute(
            "SELECT seq FROM notes WHERE id = ? AND vault = ?", (note_id, key)
        ).fetchone()
        if row is None:
            raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
        fts.delete_row(conn, row["seq"])
        conn.execute("DELETE FROM notes WHERE seq = ?", (row["seq"],))


def note_seqs(
    conn: sqlite3.Connection, vault_key: str, ids: Iterable[str]
) -> dict[str, int]:
    """內部用：已解析的 vault key 內，note id → seq。"""
    ids = list(ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, seq FROM notes WHERE vault = ? AND id IN ({placeholders})",
        (vault_key, *ids),
    ).fetchall()
    return {r["id"]: r["seq"] for r in rows}
