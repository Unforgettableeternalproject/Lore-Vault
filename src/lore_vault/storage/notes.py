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
from lore_vault.schema.chars import check_fields

from . import fts
from .db import transaction
from .errors import DuplicateRecord, NotFound, VaultRequired
from .filters import contains_clause, filter_text
from .timeutil import next_after, normalize_utc, utc_now
from .vaults import resolve_read, resolve_write, vault_clause

# update_note_if 允許改動的欄位
UPDATABLE_FIELDS = frozenset(
    {"title", "summary", "body", "topics", "links", "supersedes"}
)
# 這些欄位一變，舊 embedding 就不再代表內容，同一交易內刪掉
# （doctor 會回報缺向量、背景補算）
_EMBEDDING_FIELDS = frozenset({"title", "body"})


def note_from_row(row: sqlite3.Row) -> Note:
    """notes 表的一列 → Note（含作者欄位）。"""
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
        author=row["author"],
        principal=row["principal"],
        updated_by=row["updated_by"],
        updated_by_principal=row["updated_by_principal"],
    )


_row_to_note = note_from_row


def _note_text_fields(note: Note) -> dict[str, object]:
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "summary": note.summary,
        "topics": note.topics,
        "links": note.links,
        "supersedes": note.supersedes,
        "author": note.author,
        "updated_by": note.updated_by,
    }


def _check_vault_matches(
    conn: sqlite3.Connection, vault_key: str, note: Note, space: str
) -> None:
    if resolve_write(conn, note.vault, space=space) != vault_key:
        raise VaultRequired(
            f"note.vault {note.vault!r} 與指定的 vault {vault_key!r} 不一致"
        )


def insert_note(
    conn: sqlite3.Connection, vault: str, note: Note, *, space: str
) -> Note:
    """新增 note；回傳實際存下的版本（vault 為現行 key、時間戳已正規化）。

    `vault` 參數必填且必須與 `note.vault` 指向同一個 vault（別名會解析成現行 key）。
    含控制字元或孤立 surrogate 的欄位拋 `InvalidCharacters`（所有寫入路徑的最底層防線；
    匯入工具要先清理）。
    `note.principal` 必填（A22：每則 note 都要能追到憑證主體；缺少拋 `SchemaError`）。
    """
    check_fields(_note_text_fields(note))
    if note.principal is None:
        raise SchemaError("note.principal 必填（由服務依憑證判定）")
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        _check_vault_matches(conn, key, note, space)
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
                               supersedes, created, updated, author, principal,
                               updated_by, updated_by_principal, enqueued)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                stored.author,
                stored.principal,
                stored.updated_by,
                stored.updated_by_principal,
                # 補算入列時間一律用牆鐘：匯入與還原保留舊 `updated`，不能拿它當入列時間
                utc_now(),
            ),
        )
        seq = cursor.lastrowid
        assert seq is not None
        fts.upsert_row(
            conn, seq, stored.title, stored.summary, stored.body, stored.topics
        )
    return stored


def get_notes(
    conn: sqlite3.Connection, vault: str, ids: Sequence[str], *, space: str
) -> list[Note]:
    """依 id 取 note，保持傳入順序；不在範圍內的 id 不回傳（呼叫端可比對缺漏）。"""
    scope = resolve_read(conn, vault, space=space)
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


def get_note(conn: sqlite3.Connection, vault: str, note_id: str, *, space: str) -> Note:
    found = get_notes(conn, vault, [note_id], space=space)
    if not found:
        raise NotFound(f"vault {vault!r} 內找不到 note {note_id!r}")
    return found[0]


# list 的作者狀態篩選：named＝有自報名（含舊資料的 legacy）、missing＝未具名
AUTHOR_NAMED = "named"
AUTHOR_MISSING = "missing"
AUTHOR_STATES = (AUTHOR_NAMED, AUTHOR_MISSING)


def _note_filters(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    since: str | None,
    until: str | None,
    topics: Sequence[str] | None,
    title: str | None = None,
    author: str | None = None,
    author_state: str | None = None,
) -> tuple[list[str], list[Any]]:
    title = filter_text("title", title)
    author = filter_text("author", author)
    if author_state is not None and author_state not in AUTHOR_STATES:
        raise ValueError(f"author_state 必須是 {list(AUTHOR_STATES)} 之一")
    scope = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if since is not None:
        conditions.append("updated >= ?")
        args.append(normalize_utc(since))
    if until is not None:
        conditions.append("updated <= ?")
        args.append(normalize_utc(until))
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
    if title is not None:
        clause_sql, arg = contains_clause("title", title)
        conditions.append(clause_sql)
        args.append(arg)
    if author is not None:
        clause_sql, arg = contains_clause("author", author)
        conditions.append(clause_sql)
        args.append(arg)
    if author_state == AUTHOR_NAMED:
        conditions.append("author IS NOT NULL AND trim(author) != ''")
    elif author_state == AUTHOR_MISSING:
        conditions.append("(author IS NULL OR trim(author) = '')")
    return conditions, args


def list_notes(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    since: str | None = None,
    until: str | None = None,
    topics: Sequence[str] | None = None,
    limit: int = 50,
    cursor: tuple[str, str] | None = None,
    offset: int = 0,
    title: str | None = None,
    author: str | None = None,
    author_state: str | None = None,
) -> tuple[list[Note], tuple[str, str] | None]:
    """依 (updated, id) 由新到舊分頁。回傳 (本頁, 下一頁 cursor 或 None)。

    `since`／`until`：只列 since <= updated <= until 的 note（ISO-8601 UTC，含端點）。
    `topics`：只列至少含其中一個 topic 的 note（大小寫精確比對）；在 SQL 內、
    LIMIT 之前過濾，分頁不會因為事後過濾而少回。
    `offset`：跳過前面幾筆（頁碼分頁用）；與 `cursor` 擇一。
    `title`／`author`：標題／作者含該字串（ASCII 不分大小寫）；`author_state`：
    named（有作者）／missing（未具名）。同樣在 SQL 內、LIMIT 之前過濾。
    """
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    if offset < 0:
        raise ValueError(f"offset 不可為負，得到 {offset}")
    if offset and cursor is not None:
        raise ValueError("offset 與 cursor 只能擇一")
    conditions, args = _note_filters(
        conn,
        vault,
        space=space,
        since=since,
        until=until,
        topics=topics,
        title=title,
        author=author,
        author_state=author_state,
    )
    if cursor is not None:
        conditions.append("(updated, id) < (?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT * FROM notes WHERE {" AND ".join(conditions)}
        ORDER BY updated DESC, id DESC LIMIT ? OFFSET ?
        """,
        (*args, limit + 1, offset),
    ).fetchall()
    notes = [_row_to_note(row) for row in rows[:limit]]
    next_cursor = (notes[-1].updated, notes[-1].id) if len(rows) > limit else None
    return notes, next_cursor


def count_listed_notes(
    conn: sqlite3.Connection,
    vault: str,
    *,
    space: str,
    since: str | None = None,
    until: str | None = None,
    topics: Sequence[str] | None = None,
    title: str | None = None,
    author: str | None = None,
    author_state: str | None = None,
) -> int:
    """與 `list_notes` 相同篩選條件下的總筆數（頁碼分頁用）。"""
    conditions, args = _note_filters(
        conn,
        vault,
        space=space,
        since=since,
        until=until,
        topics=topics,
        title=title,
        author=author,
        author_state=author_state,
    )
    row = conn.execute(
        f"SELECT count(*) FROM notes WHERE {' AND '.join(conditions)}", args
    ).fetchone()
    return int(row[0])


def superseded_by(conn: sqlite3.Connection, notes: Sequence[Note]) -> dict[str, str]:
    """反向查詢更正關係：note id → 取代它的 note id（只看同一 vault）。

    多則 note 指向同一則時取 `updated` 最新者（同時間取 id 較大者）。`notes` 必須是
    已經過範圍檢查取得的 note（以各自的 vault 為界，不會帶出別的 vault）。
    """
    by_vault: dict[str, list[str]] = {}
    for note in notes:
        by_vault.setdefault(note.vault, []).append(note.id)
    found: dict[str, str] = {}
    for vault_key, ids in by_vault.items():
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join("?" * len(unique))
        rows = conn.execute(
            f"""
            SELECT id, supersedes FROM notes
            WHERE vault = ? AND supersedes IN ({placeholders}) AND id != supersedes
            ORDER BY updated DESC, id DESC
            """,
            (vault_key, *unique),
        )
        for newer, older in rows:
            found.setdefault(older, newer)
    return found


def count_notes(conn: sqlite3.Connection, vault: str, *, space: str) -> int:
    scope = resolve_read(conn, vault, space=space)
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
    space: str,
    now: str | None = None,
    editor: tuple[str | None, str] | None = None,
) -> Note | None:
    """條件更新原語（樂觀鎖）：只有資料庫中的 `updated` 字串與
    `expected_updated` 完全相同才寫入。

    - 成功：回傳新版本 note（`updated` 嚴格晚於舊版本）
    - 版本不符：回傳 None，不寫入任何東西（衝突錯誤由上層 T-21 決定怎麼回）
    - note 不在該 vault：拋 `NotFound`
    - 變動欄位含控制字元或孤立 surrogate：拋 `InvalidCharacters`，不寫入
    title／body 變動時同一交易內刪除舊 embedding。
    `editor`：(自報名或 None, principal)，寫入 `updated_by`／`updated_by_principal`
    （A22；自報名 None 就記 None，不沿用上一位）。省略＝作者欄位不動，只給不代表
    任何寫入者的內部呼叫用；服務層與匯入一律傳入。
    """
    unknown = sorted(set(changes) - UPDATABLE_FIELDS)
    if unknown:
        raise SchemaError(f"update_note_if 不可更新欄位 {unknown}")
    check_fields(changes)
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND vault = ?", (note_id, key)
        ).fetchone()
        if row is None:
            raise NotFound(f"vault {key!r} 內找不到 note {note_id!r}")
        if row["updated"] != expected_updated:
            return None
        current = _row_to_note(row)
        # 透過 dataclass 重新驗證（空標題、非字串 topics 等在這裡擋下）
        attribution: dict[str, Any] = {}
        if editor is not None:
            by, by_principal = editor
            check_fields({"updated_by": by})
            attribution = {"updated_by": by, "updated_by_principal": by_principal}
        updated = dataclasses.replace(
            current,
            **changes,
            **attribution,
            updated=next_after(current.updated, now),
        )
        cursor = conn.execute(
            """
            UPDATE notes SET title = ?, summary = ?, body = ?, topics = ?, links = ?,
                             supersedes = ?, updated = ?, updated_by = ?,
                             updated_by_principal = ?, enqueued = ?
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
                updated.updated_by,
                updated.updated_by_principal,
                # 新版本重新入列（不用 `now`：匯入更新傳的是來源的舊時間）
                utc_now(),
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


def delete_note(
    conn: sqlite3.Connection, vault: str, note_id: str, *, space: str
) -> None:
    """刪除 note 與其 FTS 列、embedding（同一交易）。"""
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
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
