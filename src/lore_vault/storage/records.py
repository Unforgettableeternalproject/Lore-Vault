"""Episode／Concept／Injection 的儲存原語。

schema 型別沒有 vault 欄；vault 由呼叫端（binding 解析結果）在寫入當下傳入並凍結，
不在讀取時依 `repo` 重算（A7）。完整記錄以 JSON 存在 `data`，`to_dict` 省略
MISSING 欄位，所以「值／None／欄位不存在」三態都能原樣讀回（spike 的 scope 事故）。

列表函式一律回傳 `(本頁, 下一頁 cursor 或 None)`：預設上限截斷時呼叫端必須看得到，
不可只回前 N 筆而不提示（與 `notes.list_notes` 相同，多抓一筆判斷是否還有下一頁）。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

from lore_vault.schema import MISSING, Concept, Episode, Injection

from .db import transaction
from .errors import DuplicateRecord
from .timeutil import normalize_opt_utc, normalize_utc, utc_now
from .vaults import resolve_read, resolve_write, vault_clause


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _check_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")


# ── Episode ─────────────────────────────────────────────────────────


def insert_episode(conn: sqlite3.Connection, vault: str, episode: Episode) -> bool:
    """寫入一輪 episode。唯一鍵 (session_id, prompt_id, turn_index)。

    重送相同內容（spool 重試）→ 回傳 False、不寫入；
    同鍵但內容不同 → 拋 `DuplicateRecord`，不默默覆蓋。
    """
    stored = dataclasses.replace(
        episode,
        started_at=normalize_opt_utc(episode.started_at),
        ended_at=normalize_opt_utc(episode.ended_at),
    )
    data = _dumps(stored.to_dict())
    with transaction(conn):
        key = resolve_write(conn, vault)
        existing = conn.execute(
            """
            SELECT vault, data FROM episodes
            WHERE session_id = ? AND prompt_id = ? AND turn_index = ?
            """,
            (stored.session_id, stored.prompt_id, stored.turn_index),
        ).fetchone()
        if existing is not None:
            if existing["vault"] == key and existing["data"] == data:
                return False
            raise DuplicateRecord(
                "episode 已存在且內容或 vault 不同："
                f"{(stored.session_id, stored.prompt_id, stored.turn_index)}"
            )
        conn.execute(
            """
            INSERT INTO episodes (vault, session_id, prompt_id, turn_index, machine,
                                  repo, started_at, ended_at, data, recorded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                stored.session_id,
                stored.prompt_id,
                stored.turn_index,
                stored.machine,
                stored.repo,
                stored.started_at,
                stored.ended_at,
                data,
                utc_now(),
            ),
        )
    return True


def list_episodes(
    conn: sqlite3.Connection,
    vault: str,
    *,
    session_id: str | None = None,
    since: str | None = None,
    limit: int = 1000,
    cursor: tuple[str, int] | None = None,
) -> tuple[list[Episode], tuple[str, int] | None]:
    """依 started_at、seq 由舊到新分頁。`since` 比對 started_at（>=）。

    回傳 (本頁, 下一頁 cursor 或 None)。started_at 可為 NULL（排最前面）；
    排序與 cursor 都用 `coalesce(started_at, '')`，避免 NULL 參與 tuple 比較時
    整列被靜默排除。
    """
    scope = resolve_read(conn, vault)
    _check_limit(limit)
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if session_id is not None:
        conditions.append("session_id = ?")
        args.append(session_id)
    if since is not None:
        conditions.append("started_at >= ?")
        args.append(normalize_utc(since))
    if cursor is not None:
        conditions.append("(coalesce(started_at, ''), seq) > (?, ?)")
        args.extend(cursor)
    rows = conn.execute(
        f"""
        SELECT data, coalesce(started_at, '') AS started_key, seq FROM episodes
        WHERE {" AND ".join(conditions)}
        ORDER BY started_key, seq LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [Episode.from_dict(json.loads(r["data"])) for r in page]
    next_cursor = (
        (page[-1]["started_key"], int(page[-1]["seq"])) if len(rows) > limit else None
    )
    return items, next_cursor


def count_episodes(conn: sqlite3.Connection, vault: str) -> int:
    scope = resolve_read(conn, vault)
    clause, params = vault_clause(scope, "vault")
    return int(
        conn.execute(
            f"SELECT count(*) FROM episodes WHERE {clause}", params
        ).fetchone()[0]
    )


# ── Concept ─────────────────────────────────────────────────────────


def _scope_columns(concept: Concept) -> tuple[str, str | None]:
    if concept.scope is MISSING:
        return "missing", None
    if concept.scope is None:
        return "global", None
    return "repo", concept.scope  # type: ignore[return-value]


def upsert_concept(conn: sqlite3.Connection, vault: str, concept: Concept) -> None:
    """新增或覆蓋 concept（蒸餾／校準會回寫同一 id）。

    已存在於另一個 vault 的 id 拒絕覆蓋——歸屬凍結，不因重跑管線而搬家。
    """
    state, scope = _scope_columns(concept)
    data = _dumps(concept.to_dict())
    with transaction(conn):
        key = resolve_write(conn, vault)
        owner = conn.execute(
            "SELECT vault FROM concepts WHERE id = ?", (concept.id,)
        ).fetchone()
        if owner is not None and owner["vault"] != key:
            raise DuplicateRecord(
                f"concept {concept.id!r} 已屬於 vault {owner['vault']!r}"
            )
        conn.execute(
            """
            INSERT INTO concepts (id, vault, kind, scope_state, scope, data, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET kind = excluded.kind,
                scope_state = excluded.scope_state, scope = excluded.scope,
                data = excluded.data, updated = excluded.updated
            """,
            (concept.id, key, concept.kind, state, scope, data, utc_now()),
        )


def get_concepts(
    conn: sqlite3.Connection, vault: str, ids: list[str] | tuple[str, ...]
) -> list[Concept]:
    scope = resolve_read(conn, vault)
    if isinstance(ids, str):
        raise TypeError("ids 必須是清單，不可傳單一字串")
    if not ids:
        return []
    clause, params = vault_clause(scope, "vault")
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, data FROM concepts WHERE id IN ({placeholders}) AND {clause}",
        (*ids, *params),
    ).fetchall()
    by_id = {r["id"]: Concept.from_dict(json.loads(r["data"])) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def list_concepts(
    conn: sqlite3.Connection,
    vault: str,
    *,
    limit: int = 10000,
    cursor: str | None = None,
) -> tuple[list[Concept], str | None]:
    """依 id 排序分頁；回傳 (本頁, 下一頁 cursor（最後一筆的 id）或 None)。"""
    scope = resolve_read(conn, vault)
    _check_limit(limit)
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if cursor is not None:
        conditions.append("id > ?")
        args.append(cursor)
    rows = conn.execute(
        f"""
        SELECT id, data FROM concepts WHERE {" AND ".join(conditions)}
        ORDER BY id LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [Concept.from_dict(json.loads(r["data"])) for r in page]
    return items, (page[-1]["id"] if len(rows) > limit else None)


# ── Injection ───────────────────────────────────────────────────────


def insert_injection(
    conn: sqlite3.Connection,
    vault: str,
    injection: Injection,
    *,
    recorded: str | None = None,
) -> None:
    rec = normalize_utc(recorded) if recorded is not None else utc_now()
    with transaction(conn):
        key = resolve_write(conn, vault)
        conn.execute(
            """
            INSERT INTO injections (vault, session_id, prompt_id, prompt_fingerprint,
                                    data, recorded)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                injection.session_id,
                injection.prompt_id,
                injection.prompt_fingerprint,
                _dumps(injection.to_dict()),
                rec,
            ),
        )


def list_injections(
    conn: sqlite3.Connection,
    vault: str,
    *,
    session_id: str | None = None,
    limit: int = 10000,
    cursor: int | None = None,
) -> tuple[list[Injection], int | None]:
    """依寫入順序分頁；回傳 (本頁, 下一頁 cursor（最後一筆的 seq）或 None)。"""
    scope = resolve_read(conn, vault)
    _check_limit(limit)
    clause, params = vault_clause(scope, "vault")
    conditions = [clause]
    args: list[Any] = [*params]
    if session_id is not None:
        conditions.append("session_id = ?")
        args.append(session_id)
    if cursor is not None:
        conditions.append("seq > ?")
        args.append(cursor)
    rows = conn.execute(
        f"""
        SELECT seq, data FROM injections WHERE {" AND ".join(conditions)}
        ORDER BY seq LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [Injection.from_dict(json.loads(r["data"])) for r in page]
    return items, (int(page[-1]["seq"]) if len(rows) > limit else None)
