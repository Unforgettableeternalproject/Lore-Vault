"""UI 管理端點的儲存原語（T-70～T-75）：vault 列表與編輯、別名、墓碑列表、
文件人工重試、concept 瀏覽、episode 統計，以及對應的 doctor 對帳。

範圍規則同其他讀寫（A5／A18）：一律經 `resolve_read`／`resolve_write`／
`vault_clause`，vault 在別的 space 與不存在相同（`UnknownVault`）。

墓碑的 space：vault 還在就用 `vaults.space`；vault 已刪除時依 key 前綴推斷
（非 dev 的 key 一律 `<space>/` 開頭——`space.key_prefix_agreement`；set-space
會一併改寫墓碑的 vault 欄）。已刪的 dev vault 若 key 恰好以 `lore/`／`personal/`
開頭會被誤判，實務上 binding 算出的 key 不會如此。

concept／episode 只屬於 dev（A18）；對 lore／personal 查詢時範圍內沒有資料，
自然回空。回傳一律不含對話原文（`user_text`、`assistant_text`、concept 的
probe／why／evidence 等）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lore_vault.schema import canonical_key
from lore_vault.schema.chars import check_fields

from . import documents as store
from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .errors import NotFound, StorageError, UnknownVault, VaultRequired
from .timeutil import normalize_utc
from .vaults import (
    ALL_VAULTS,
    check_key_prefix,
    resolve_read,
    resolve_write,
    validate_space,
    vault_clause,
)

# 人工重排抽取的上限（每份文件）。抽取錯誤多為確定性（加密、損毀），重試多半同結果
MAX_MANUAL_RETRIES = 3

KIND_NOTE = "note"
KIND_DOCUMENT = "document"
TOMBSTONE_KINDS = frozenset({KIND_NOTE, KIND_DOCUMENT})

SCOPE_STATES = frozenset({"repo", "global", "missing"})


class AliasConflict(StorageError):
    """別名已被使用。`existing` 只在佔用者屬於同一 space 時提供（不跨 space 透露）。"""

    def __init__(self, message: str, existing: str | None = None) -> None:
        super().__init__(message)
        self.existing = existing


class CannotRemoveKey(StorageError, ValueError):
    """要移除的是 vault 的正式 key，不是別名。"""


class RetryRefused(StorageError):
    """文件不是 failed，或人工重試已達上限。`reason`：`not_failed`／`retry_limit`。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ── vault 列表與編輯（T-70）──


@dataclass(frozen=True)
class VaultSummary:
    key: str
    display: str
    kind: str
    space: str
    origin: str
    aliases: tuple[str, ...]
    note_count: int
    document_count: int
    created: str
    last_updated: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display": self.display,
            "kind": self.kind,
            "space": self.space,
            "origin": self.origin,
            "aliases": list(self.aliases),
            "note_count": self.note_count,
            "document_count": self.document_count,
            "created": self.created,
            "last_updated": self.last_updated,
        }


def _summaries(conn: sqlite3.Connection, where: str, params: tuple) -> list[Any]:
    has_docs = _has_table(conn, "documents")
    doc_count = (
        "(SELECT count(*) FROM documents d WHERE d.vault = v.key)" if has_docs else "0"
    )
    doc_updated = (
        "(SELECT max(d.updated) FROM documents d WHERE d.vault = v.key)"
        if has_docs
        else "NULL"
    )
    rows = conn.execute(
        f"""
        SELECT v.key, v.display, v.kind, v.space, v.origin, v.created,
               (SELECT count(*) FROM notes n WHERE n.vault = v.key) AS note_count,
               {doc_count} AS document_count,
               (SELECT max(n.updated) FROM notes n WHERE n.vault = v.key)
                   AS notes_updated,
               {doc_updated} AS docs_updated
        FROM vaults v WHERE {where}
        ORDER BY v.key
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        aliases = tuple(
            a[0]
            for a in conn.execute(
                "SELECT alias FROM vault_aliases WHERE vault = ? ORDER BY alias",
                (r["key"],),
            )
        )
        stamps = [s for s in (r["notes_updated"], r["docs_updated"]) if s]
        out.append(
            VaultSummary(
                key=r["key"],
                display=r["display"],
                kind=r["kind"],
                space=r["space"],
                origin=r["origin"],
                aliases=aliases,
                note_count=int(r["note_count"]),
                document_count=int(r["document_count"]),
                created=r["created"],
                last_updated=max(stamps) if stamps else None,
            )
        )
    return out


def list_vault_summaries(conn: sqlite3.Connection, *, space: object) -> list[Any]:
    """`space` 內所有 vault 與筆數、最近更新。

    最近更新＝note／文件最大的 updated，都沒有為 None。
    """
    checked = validate_space(space)
    return _summaries(conn, "v.space = ?", (checked,))


def vault_summary(conn: sqlite3.Connection, vault: object, *, space: object) -> Any:
    key = resolve_write(conn, vault, space=space)
    return _summaries(conn, "v.key = ?", (key,))[0]


def update_display(
    conn: sqlite3.Connection, vault: object, display: object, *, space: object
) -> Any:
    """只改顯示名稱（key、kind、space、別名不動）。"""
    if not isinstance(display, str) or not display.strip():
        raise ValueError("display 不可為空")
    if display != display.strip():
        raise ValueError(f"display 前後不可有空白：{display!r}")
    check_fields({"display": display})
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        conn.execute("UPDATE vaults SET display = ? WHERE key = ?", (display, key))
    return vault_summary(conn, key, space=space)


# ── 別名（T-71）──


def _alias_owner(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    """name 已是某 vault 的 key 或別名時回傳 (vault key, space)。"""
    row = conn.execute(
        "SELECT key, space FROM vaults WHERE key = ?", (name,)
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    row = conn.execute(
        """
        SELECT a.vault, v.space FROM vault_aliases a JOIN vaults v ON v.key = a.vault
        WHERE a.alias = ?
        """,
        (name,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _clean_alias(alias: object) -> str:
    if not isinstance(alias, str) or not alias.strip():
        raise ValueError("alias 不可為空")
    if alias != alias.strip():
        raise ValueError(f"alias 前後不可有空白：{alias!r}")
    if alias == ALL_VAULTS:
        raise VaultRequired("'*' 保留給跨 vault 查詢，不可當別名")
    return canonical_key(alias)


def add_alias(
    conn: sqlite3.Connection, vault: object, alias: object, *, space: object
) -> Any:
    """為 vault 加一個別名。前綴規則同建立（非 dev 須 `<space>/` 開頭）；
    別名已是任何 vault 的 key 或別名 → `AliasConflict`（key 全域唯一）。"""
    checked = validate_space(space)
    name = _clean_alias(alias)
    check_key_prefix(checked, name)
    with transaction(conn):
        key = resolve_write(conn, vault, space=checked)
        owner = _alias_owner(conn, name)
        if owner is not None:
            owner_key, owner_space = owner
            if owner_space == checked:
                raise AliasConflict(
                    f"{name!r} 已被 vault {owner_key!r} 使用（key 或別名）", owner_key
                )
            raise AliasConflict(f"{name!r} 已被使用（key 或別名全域唯一）")
        conn.execute(
            "INSERT INTO vault_aliases (alias, vault) VALUES (?, ?)", (name, key)
        )
    return vault_summary(conn, key, space=checked)


def remove_alias(
    conn: sqlite3.Connection, vault: object, alias: object, *, space: object
) -> Any:
    """移除 vault 的一個別名；不可移除正式 key（`CannotRemoveKey`）。
    別名不屬於這個 vault → `NotFound`。"""
    checked = validate_space(space)
    name = _clean_alias(alias)
    with transaction(conn):
        key = resolve_write(conn, vault, space=checked)
        if name == key:
            raise CannotRemoveKey(f"{name!r} 是 vault 的正式 key，不是別名，不可移除")
        removed = conn.execute(
            "DELETE FROM vault_aliases WHERE alias = ? AND vault = ?", (name, key)
        ).rowcount
        if removed != 1:
            raise NotFound(f"vault {key!r} 沒有別名 {name!r}")
    return vault_summary(conn, key, space=checked)


# ── 墓碑列表（T-73）──

_SPACE_OF = """
    coalesce(
        (SELECT v.space FROM vaults v WHERE v.key = {col}),
        CASE WHEN substr({col}, 1, 5) = 'lore/' THEN 'lore'
             WHEN substr({col}, 1, 9) = 'personal/' THEN 'personal'
             ELSE 'dev' END
    )
"""


def tombstone_space(conn: sqlite3.Connection, vault_key: str) -> str:
    """墓碑所屬 space（vault 在就用它的 space，已刪就依前綴推斷）。"""
    sql = "SELECT " + _SPACE_OF.format(col="?")
    return conn.execute(sql, (vault_key, vault_key, vault_key)).fetchone()[0]


def _tombstone_vault_filter(
    conn: sqlite3.Connection, vault: object, space: str
) -> str | None:
    """墓碑列表的 vault 參數：`"*"` → None（space 內全部）；vault 還在就解析別名；
    已刪的 vault 以原 key 比對，但必須有該 space 的墓碑，否則 `UnknownVault`。"""
    if vault == ALL_VAULTS:
        resolve_read(conn, vault, space=space)  # 參數驗證
        return None
    try:
        return resolve_write(conn, vault, space=space)
    except UnknownVault:
        pass
    key = canonical_key(str(vault))
    exists = conn.execute("SELECT 1 FROM vaults WHERE key = ?", (key,)).fetchone()
    if exists is None and tombstone_space(conn, key) == space:
        for table in ("note_tombstones", "document_tombstones"):
            if (
                _has_table(conn, table)
                and conn.execute(
                    f"SELECT 1 FROM {table} WHERE vault = ? LIMIT 1", (key,)
                ).fetchone()
            ):
                return key
    raise UnknownVault(f"space {space!r} 內 vault 不存在：{key!r}")


def list_tombstones(
    conn: sqlite3.Connection,
    vault: object,
    *,
    space: object,
    kinds: list[str] | None = None,
    limit: int = 50,
    cursor: tuple[str, str, str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, str, str] | None]:
    """note 與文件墓碑合併，依 (deleted_at, kind, id) 由新到舊分頁。只回 metadata。"""
    checked = validate_space(space)
    wanted = set(kinds) if kinds is not None else set(TOMBSTONE_KINDS)
    unknown = wanted - TOMBSTONE_KINDS
    if unknown or not wanted:
        raise ValueError(f"kinds 必須是 {sorted(TOMBSTONE_KINDS)} 的非空子集")
    if not 0 < limit <= 500:
        raise ValueError(f"limit 必須在 1～500，得到 {limit}")
    key = _tombstone_vault_filter(conn, vault, checked)
    parts: list[str] = []
    args: list[Any] = []
    if KIND_NOTE in wanted:
        # v12 起有內容快照：列出標題供辨識（只取標題，不取內文）
        has_snap = _has_column(conn, "note_tombstones", "snapshot")
        title = "json_extract(snapshot, '$.title')" if has_snap else "NULL"
        snap = "snapshot IS NOT NULL" if has_snap else "0"
        parts.append(
            f"""
            SELECT 'note' AS kind, note_id AS id, vault, deleted_at, reason,
                   source, NULL AS sha256, NULL AS filename,
                   {title} AS title, ({snap}) AS has_snapshot,
                   ({_SPACE_OF.format(col="vault")}) AS space
            FROM note_tombstones
            """
        )
    if KIND_DOCUMENT in wanted and _has_table(conn, "document_tombstones"):
        has_meta = _has_column(conn, "document_tombstones", "filename")
        parts.append(
            f"""
            SELECT 'document' AS kind, document_id AS id, vault, deleted_at, reason,
                   NULL AS source, sha256, {"filename" if has_meta else "NULL"},
                   NULL AS title, 0 AS has_snapshot,
                   ({_SPACE_OF.format(col="vault")}) AS space
            FROM document_tombstones
            """
        )
    if not parts:
        return [], None
    conditions = ["space = ?"]
    args.append(checked)
    if key is not None:
        conditions.append("vault = ?")
        args.append(key)
    if cursor is not None:
        conditions.append("(deleted_at, kind, id) < (?, ?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT * FROM ({" UNION ALL ".join(parts)})
        WHERE {" AND ".join(conditions)}
        ORDER BY deleted_at DESC, kind DESC, id DESC
        LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = []
    for r in page:
        item: dict[str, Any] = {
            "kind": r["kind"],
            "id": r["id"],
            "vault": r["vault"],
            "vault_exists": conn.execute(
                "SELECT 1 FROM vaults WHERE key = ?", (r["vault"],)
            ).fetchone()
            is not None,
            "deleted_at": r["deleted_at"],
            "reason": r["reason"],
        }
        if r["kind"] == KIND_NOTE:
            # 有快照（v12 起）且 vault 還在 → undelete 以原內容還原（restorable）；
            # 沒有快照的舊墓碑 undelete 只移除墓碑，有匯入來源時重跑匯入才會回來
            item["source"] = r["source"]
            item["title"] = r["title"]
            item["restorable"] = bool(r["has_snapshot"]) and item["vault_exists"]
            item["reimportable"] = r["source"] is not None
        else:
            item["sha256"] = r["sha256"]
            item["filename"] = r["filename"]
            item["restorable"] = r["filename"] is not None and item["vault_exists"]
        items.append(item)
    next_cursor = (
        (page[-1]["deleted_at"], page[-1]["kind"], page[-1]["id"])
        if len(rows) > limit
        else None
    )
    return items, next_cursor


def note_tombstone_in_space(
    conn: sqlite3.Connection, note_id: str, *, space: object
) -> dict[str, Any]:
    """查 note 墓碑並限定在 space 內；別的 space 與不存在相同（`NotFound`）。"""
    checked = validate_space(space)
    row = conn.execute(
        "SELECT vault FROM note_tombstones WHERE note_id = ?", (note_id,)
    ).fetchone()
    if row is None or tombstone_space(conn, row[0]) != checked:
        raise NotFound(f"note {note_id!r} 沒有墓碑")
    return {"vault": row[0]}


def document_tombstone_in_space(
    conn: sqlite3.Connection, document_id: str, *, space: object
) -> None:
    """文件墓碑存在且屬於 space；別的 space 與不存在相同（`NotFound`）。"""
    checked = validate_space(space)
    row = conn.execute(
        "SELECT vault FROM document_tombstones WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None or tombstone_space(conn, row[0]) != checked:
        raise NotFound(f"文件 {document_id!r} 沒有墓碑")


# ── 文件人工重試（T-74）──


def retry_document(
    conn: sqlite3.Connection, vault: object, document_id: str, *, space: object
) -> tuple[Any, int]:
    """failed 文件重排抽取：沿用上傳的 `reset_for_retry`（改回 pending、清錯誤與
    嘗試紀錄，檔名／MIME 不變），人工次數 +1，達 `MAX_MANUAL_RETRIES` 拒絕。
    回傳 (Document, 重試後的人工次數)。"""
    with transaction(conn):
        key = resolve_write(conn, vault, space=space)
        row = conn.execute(
            "SELECT status, filename, mime, manual_retries FROM documents "
            "WHERE id = ? AND vault = ?",
            (document_id, key),
        ).fetchone()
        if row is None:
            raise NotFound(f"vault {key!r} 內找不到文件 {document_id!r}")
        if row["status"] != store.STATUS_FAILED:
            raise RetryRefused(
                f"文件 {document_id!r} 狀態為 {row['status']}，只有 failed 可重試",
                "not_failed",
            )
        used = int(row["manual_retries"])
        if used >= MAX_MANUAL_RETRIES:
            raise RetryRefused(
                f"文件 {document_id!r} 已人工重試 {used} 次"
                f"（上限 {MAX_MANUAL_RETRIES}）；"
                "請修正原始檔後重新上傳",
                "retry_limit",
            )
        doc = store.reset_for_retry(
            conn, document_id, filename=row["filename"], mime=row["mime"]
        )
        conn.execute(
            "UPDATE documents SET manual_retries = manual_retries + 1 WHERE id = ?",
            (document_id,),
        )
        return doc, used + 1


# ── concept 瀏覽與 episode 統計（T-75）──


def query_concepts(
    conn: sqlite3.Connection,
    vault: object,
    *,
    space: object,
    scope: str | None = None,
    scope_state: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    cursor: tuple[str, str] | None = None,
    since: str | None = None,
    until: str | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    """依 (updated, id) 由新到舊分頁。只回 metadata 與 statement（白名單欄位）。

    `scope`：不分大小寫比對 repo scope；`scope_state`：repo／global／missing。
    `since`／`until`：updated 區間（含端點）；`offset`：頁碼分頁，與 `cursor` 擇一。
    """
    if not 0 < limit <= 200:
        raise ValueError(f"limit 必須在 1～200，得到 {limit}")
    if offset < 0:
        raise ValueError(f"offset 不可為負，得到 {offset}")
    if offset and cursor is not None:
        raise ValueError("offset 與 cursor 只能擇一")
    conditions, args = _concept_filters(
        conn,
        vault,
        space=space,
        scope=scope,
        scope_state=scope_state,
        kind=kind,
        since=since,
        until=until,
    )
    if cursor is not None:
        conditions.append("(updated, id) < (?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT id, vault, kind, scope_state, scope, data, updated FROM concepts
        WHERE {" AND ".join(conditions)}
        ORDER BY updated DESC, id DESC LIMIT ? OFFSET ?
        """,
        (*args, limit + 1, offset),
    ).fetchall()
    page = rows[:limit]
    items = [_concept_item(r) for r in page]
    next_cursor = (page[-1]["updated"], page[-1]["id"]) if len(rows) > limit else None
    return items, next_cursor


def count_concepts(
    conn: sqlite3.Connection,
    vault: object,
    *,
    space: object,
    scope: str | None = None,
    scope_state: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """與 `query_concepts` 相同篩選條件下的總筆數。"""
    conditions, args = _concept_filters(
        conn,
        vault,
        space=space,
        scope=scope,
        scope_state=scope_state,
        kind=kind,
        since=since,
        until=until,
    )
    row = conn.execute(
        f"SELECT count(*) FROM concepts WHERE {' AND '.join(conditions)}", args
    ).fetchone()
    return int(row[0])


def _concept_filters(
    conn: sqlite3.Connection,
    vault: object,
    *,
    space: object,
    scope: str | None,
    scope_state: str | None,
    kind: str | None,
    since: str | None,
    until: str | None,
) -> tuple[list[str], list[Any]]:
    scope_range = resolve_read(conn, vault, space=space)
    if scope_state is not None and scope_state not in SCOPE_STATES:
        raise ValueError(f"scope_state 必須是 {sorted(SCOPE_STATES)} 之一")
    clause, params = vault_clause(scope_range, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if scope is not None:
        conditions.append("scope = ? COLLATE NOCASE")
        args.append(scope)
    if scope_state is not None:
        conditions.append("scope_state = ?")
        args.append(scope_state)
    if kind is not None:
        conditions.append("kind = ?")
        args.append(kind)
    if since is not None:
        conditions.append("updated >= ?")
        args.append(normalize_utc(since))
    if until is not None:
        conditions.append("updated <= ?")
        args.append(normalize_utc(until))
    return conditions, args


def _concept_item(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(row["data"])
    usability = data.get("usability")
    anchors = data.get("anchors") or []
    return {
        "id": row["id"],
        "vault": row["vault"],
        "kind": row["kind"],
        "scope": row["scope"],
        "scope_state": row["scope_state"],
        "statement": data.get("statement"),
        "anchors": [a for a in anchors if isinstance(a, str)],
        "surprisal": data.get("surprisal"),
        # usability 另含 evidence／note（可能引用對話），只回判定
        "usability_verdict": (
            usability.get("verdict") if isinstance(usability, dict) else None
        ),
        "updated": row["updated"],
    }


def episode_summary(
    conn: sqlite3.Connection, vault: object, *, space: object
) -> dict[str, Any]:
    """各 machine／vault 的 episode 筆數與最近時間（recorded＝收料時間、
    started_at＝對話時間）。不讀 data 欄，不含任何對話原文。"""
    scope_range = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope_range, "vault")

    def group(column: str) -> list[dict[str, Any]]:
        return [
            {
                column: r[0],
                "episodes": int(r[1]),
                "last_recorded": r[2],
                "last_started": r[3],
            }
            for r in conn.execute(
                f"""
                SELECT {column}, count(*), max(recorded), max(started_at)
                FROM episodes WHERE {clause}
                GROUP BY {column} ORDER BY {column}
                """,
                params,
            )
        ]

    total = conn.execute(
        f"SELECT count(*), max(recorded) FROM episodes WHERE {clause}", params
    ).fetchone()
    return {
        "total": int(total[0]),
        "last_recorded": total[1],
        "by_machine": group("machine"),
        "by_vault": group("vault"),
    }


# ── 標籤清單 ──


def topic_counts(
    conn: sqlite3.Connection, vault: object, *, space: object
) -> tuple[str | None, list[dict[str, Any]]]:
    """範圍內 note 的 topic 與使用筆數（依筆數由多到少、同數依名稱）。

    `vault="*"` 為 space 內全部 vault（只解除 vault 這一層）；vault 在別的 space
    與不存在相同（`UnknownVault`）。回傳 (解析後的 vault key 或 None, 清單)。
    """
    scope = resolve_read(conn, vault, space=space)
    clause, params = vault_clause(scope, "notes.vault")
    rows = conn.execute(
        f"""
        SELECT t.value AS topic, count(*) AS n
        FROM notes, json_each(notes.topics) AS t
        WHERE {clause}
        GROUP BY t.value ORDER BY n DESC, t.value
        """,
        params,
    ).fetchall()
    return scope.key, [{"topic": r["topic"], "count": int(r["n"])} for r in rows]


# ── doctor 對帳 ──


def alias_integrity(conn: sqlite3.Connection) -> Reconciliation:
    """別名不得等於任何 vault 的正式 key（解析會有歧義），也不得指向不存在的 vault
    （外鍵關閉時可能發生）。兩者都只靠寫入路徑守，DB 沒有約束。"""
    shadow = conn.execute(
        """
        SELECT a.alias, a.vault FROM vault_aliases a
        JOIN vaults v ON v.key = a.alias ORDER BY a.alias
        """
    ).fetchall()
    dangling = conn.execute(
        """
        SELECT alias, vault FROM vault_aliases
        WHERE vault NOT IN (SELECT key FROM vaults) ORDER BY alias
        """
    ).fetchall()
    total = int(conn.execute("SELECT count(*) FROM vault_aliases").fetchone()[0])
    counts = {"aliases": total, "shadowing_key": len(shadow), "dangling": len(dangling)}
    if not shadow and not dangling:
        return Reconciliation("pass", f"{total} 個別名皆唯一且指向現存 vault", counts)
    details = [f"別名 {r[0]!r}（屬 {r[1]}）與某 vault 的 key 相同" for r in shadow]
    details += [f"別名 {r[0]!r} 指向不存在的 vault {r[1]!r}" for r in dangling]
    return Reconciliation(
        "fail",
        f"{len(shadow) + len(dangling)} 個別名與 key 衝突或指向不存在的 vault",
        counts,
        tuple(details[:MAX_DETAILS]),
    )


def tombstones_disjoint(conn: sqlite3.Connection) -> Reconciliation:
    """墓碑與現行表沒有重複 id（undelete 必須同交易刪墓碑；否則重跑匯入會跳過
    一則其實存在的 note、文件列表與墓碑列表互相矛盾）。"""
    note_dup = [
        r[0]
        for r in conn.execute(
            "SELECT note_id FROM note_tombstones "
            "WHERE note_id IN (SELECT id FROM notes) ORDER BY note_id"
        )
    ]
    doc_dup: list[str] = []
    if _has_table(conn, "document_tombstones"):
        doc_dup = [
            r[0]
            for r in conn.execute(
                "SELECT document_id FROM document_tombstones "
                "WHERE document_id IN (SELECT id FROM documents) ORDER BY document_id"
            )
        ]
    counts = {"note_overlap": len(note_dup), "document_overlap": len(doc_dup)}
    if not note_dup and not doc_dup:
        return Reconciliation("pass", "墓碑與現行 note／文件無重複 id", counts)
    details = [f"note {i} 同時存在於墓碑與 notes" for i in note_dup]
    details += [f"文件 {i} 同時存在於墓碑與 documents" for i in doc_dup]
    return Reconciliation(
        "fail",
        f"{len(note_dup) + len(doc_dup)} 筆 id 同時在墓碑與現行表",
        counts,
        tuple(details[:MAX_DETAILS]),
    )


def note_attribution(conn: sqlite3.Connection) -> Reconciliation:
    """每則 note 都有 principal 與 updated_by_principal（A22）。欄位可為 NULL、沒有
    DEFAULT（漏設不會被默默記成某人），靠寫入路徑與這項對帳把關。"""
    missing = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM notes WHERE principal IS NULL "
            "OR updated_by_principal IS NULL ORDER BY id"
        )
    ]
    total = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
    unnamed = int(
        conn.execute("SELECT count(*) FROM notes WHERE author IS NULL").fetchone()[0]
    )
    counts = {"notes": total, "missing_principal": len(missing), "unnamed": unnamed}
    if not missing:
        return Reconciliation(
            "pass", f"{total} 則 note 皆有 principal（未具名 {unnamed} 則）", counts
        )
    return Reconciliation(
        "fail",
        f"{len(missing)} 則 note 缺 principal／updated_by_principal",
        counts,
        tuple(f"note {i} 缺 principal" for i in missing[:MAX_DETAILS]),
    )


_SNAPSHOT_KEYS = ("id", "vault", "title", "body", "created", "updated")


def tombstone_snapshots(conn: sqlite3.Connection) -> Reconciliation:
    """note 墓碑的內容快照可解析、id 與墓碑相同、還原必要欄位齊全（restore 依賴它）。
    快照內的 vault 不比對：換 space 只改寫墓碑的 vault 欄，還原以該欄為準。"""
    bad: list[str] = []
    with_snapshot = legacy = 0
    for note_id, raw in conn.execute(
        "SELECT note_id, snapshot FROM note_tombstones ORDER BY note_id"
    ):
        if raw is None:
            legacy += 1
            continue
        with_snapshot += 1
        try:
            data = json.loads(raw)
        except ValueError:
            bad.append(f"note {note_id} 的墓碑快照不是合法 JSON")
            continue
        if not isinstance(data, dict) or data.get("id") != note_id:
            bad.append(f"note {note_id} 的墓碑快照 id 不符")
            continue
        lacking = [k for k in _SNAPSHOT_KEYS if not isinstance(data.get(k), str)]
        if lacking:
            bad.append(f"note {note_id} 的墓碑快照缺欄位 {lacking}")
    counts = {"with_snapshot": with_snapshot, "legacy": legacy, "invalid": len(bad)}
    if not bad:
        return Reconciliation(
            "pass",
            f"{with_snapshot} 筆墓碑快照可還原（無快照的舊墓碑 {legacy} 筆）",
            counts,
        )
    return Reconciliation(
        "fail",
        f"{len(bad)} 筆墓碑快照無法用於還原",
        counts,
        tuple(bad[:MAX_DETAILS]),
    )


def tombstone_stats(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    warn_age_days: float = 0.0,
    warn_bytes: int = 0,
) -> Reconciliation:
    """資訊項：墓碑筆數、note 快照總位元組（UTF-8）、最舊一筆的年齡。

    墓碑與快照永久保留、只能由 `cli.admin purge-tombstones` 明確清除（不做自動清除）。
    門檻為 0 代表不警告（預設）；`warn_age_days`＞0 時最舊一筆超過即 warn，
    `warn_bytes`＞0 時快照總位元組超過即 warn。永遠不 fail。
    """
    notes = documents = snapshot_bytes = 0
    stamps: list[str] = []
    if _has_table(conn, "note_tombstones"):
        size = (
            "length(CAST(snapshot AS BLOB))"
            if _has_column(conn, "note_tombstones", "snapshot")
            else "0"
        )
        row = conn.execute(
            f"SELECT count(*), coalesce(sum({size}), 0), min(deleted_at) "
            "FROM note_tombstones"
        ).fetchone()
        notes, snapshot_bytes = int(row[0]), int(row[1])
        if row[2] is not None:
            stamps.append(row[2])
    if _has_table(conn, "document_tombstones"):
        row = conn.execute(
            "SELECT count(*), min(deleted_at) FROM document_tombstones"
        ).fetchone()
        documents = int(row[0])
        if row[1] is not None:
            stamps.append(row[1])
    oldest_age = 0
    if stamps:
        oldest = datetime.fromisoformat(min(stamps))
        oldest_age = max(0, int((now - oldest).total_seconds()))
    counts = {
        "note_tombstones": notes,
        "document_tombstones": documents,
        "snapshot_bytes": snapshot_bytes,
        "oldest_age_seconds": oldest_age,
    }
    summary = (
        f"墓碑 note {notes}／文件 {documents} 筆，快照 {snapshot_bytes} 位元組"
        + (f"，最舊 {oldest_age / 86400:.1f} 天" if stamps else "")
    )
    reasons: list[str] = []
    if warn_age_days > 0 and stamps and oldest_age > warn_age_days * 86400:
        reasons.append(f"最舊墓碑超過 {warn_age_days:g} 天")
    if warn_bytes > 0 and snapshot_bytes > warn_bytes:
        reasons.append(f"快照總量超過 {warn_bytes} 位元組")
    if reasons:
        return Reconciliation(
            "warn",
            f"{summary}；{'、'.join(reasons)}（可用 cli.admin purge-tombstones 清除）",
            counts,
        )
    return Reconciliation("pass", summary, counts)


# ── 共用 ──


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )
