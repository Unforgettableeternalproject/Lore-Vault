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

from lore_vault.schema import MISSING, SPACE_DEV, Concept, Episode, Injection

from .db import transaction
from .errors import DuplicateRecord, NotFound
from .timeutil import normalize_opt_utc, normalize_utc, utc_now
from .vaults import resolve_read, resolve_write, vault_clause

# episode／concept／injection 只屬於 dev（A18／設計 2.5）：hook 與 spike 管線
# 不感知 space，範圍固定 SPACE_DEV，客戶端不帶 space。


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
        key = resolve_write(conn, vault, space=SPACE_DEV)
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
    rows, next_cursor = list_episode_rows(
        conn, vault, session_id=session_id, since=since, limit=limit, cursor=cursor
    )
    return [episode for _, episode in rows], next_cursor


def list_episode_rows(
    conn: sqlite3.Connection,
    vault: str,
    *,
    session_id: str | None = None,
    since: str | None = None,
    limit: int = 1000,
    cursor: tuple[str, int] | None = None,
) -> tuple[list[tuple[str, Episode]], tuple[str, int] | None]:
    """同 `list_episodes`，但每筆附上寫入當下凍結的 vault key：`(vault, Episode)`。

    Episode schema 沒有 vault 欄；跨 vault（`"*"`）讀取的管線要靠這個把
    concept 寫回正確的 vault。
    """
    scope = resolve_read(conn, vault, space=SPACE_DEV)
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
        SELECT vault, data, coalesce(started_at, '') AS started_key, seq
        FROM episodes
        WHERE {" AND ".join(conditions)}
        ORDER BY started_key, seq LIMIT ?
        """,
        (*args, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [(r["vault"], Episode.from_dict(json.loads(r["data"]))) for r in page]
    next_cursor = (
        (page[-1]["started_key"], int(page[-1]["seq"])) if len(rows) > limit else None
    )
    return items, next_cursor


def list_episode_rows_after_seq(
    conn: sqlite3.Connection,
    vault: str,
    *,
    after_seq: int,
    limit: int = 1000,
) -> tuple[list[tuple[str, int, Episode]], int | None, int, int]:
    """增量讀取：`seq > after_seq`，依 seq 由小到大（主機管線的 episode 快取用）。

    回傳 `(本頁 [(vault, seq, Episode)], 下一頁的 after_seq 或 None, max_seq, total)`。
    `max_seq` 是**先讀出**的範圍內最大 seq（沒有資料為 0），本頁與 `total` 都只算
    `seq <= max_seq`：讀取期間新寫入的列不會讓 total 與本頁對不上。

    seq 是 INTEGER PRIMARY KEY、episodes 只插入不刪改，SQLite 單一寫者使 seq 依序可見，
    所以 `seq > 水位` 不會漏掉延遲到貨的舊對話（`started_at` 很舊、很晚才收料的
    遠端 episode）。
    """
    scope = resolve_read(conn, vault, space=SPACE_DEV)
    _check_limit(limit)
    if after_seq < 0:
        raise ValueError(f"after_seq 不可為負，得到 {after_seq}")
    clause, params = vault_clause(scope, "vault")
    max_seq = int(
        conn.execute(
            f"SELECT coalesce(max(seq), 0) FROM episodes WHERE {clause}", params
        ).fetchone()[0]
    )
    total = int(
        conn.execute(
            f"SELECT count(*) FROM episodes WHERE {clause} AND seq <= ?",
            (*params, max_seq),
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT vault, seq, data FROM episodes
        WHERE {clause} AND seq > ? AND seq <= ?
        ORDER BY seq LIMIT ?
        """,
        (*params, after_seq, max_seq, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [
        (r["vault"], int(r["seq"]), Episode.from_dict(json.loads(r["data"])))
        for r in page
    ]
    next_after = int(page[-1]["seq"]) if len(rows) > limit else None
    return items, next_after, max_seq, total


def count_episodes(conn: sqlite3.Connection, vault: str) -> int:
    scope = resolve_read(conn, vault, space=SPACE_DEV)
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


CONCEPT_CREATED = "created"
CONCEPT_UPDATED = "updated"
CONCEPT_UNCHANGED = "unchanged"

UPSERT_MODES = frozenset({"upsert", "create", "update"})


def concept_owner(conn: sqlite3.Connection, concept_id: str) -> str | None:
    """concept id 目前所屬的 vault key；不存在回 None（id 是全域唯一鍵）。"""
    row = conn.execute(
        "SELECT vault FROM concepts WHERE id = ?", (concept_id,)
    ).fetchone()
    return None if row is None else row["vault"]


def upsert_concept(
    conn: sqlite3.Connection, vault: str, concept: Concept, *, mode: str = "upsert"
) -> str:
    """新增或覆蓋 concept（蒸餾／校準會回寫同一 id）；回傳 created／updated／unchanged。

    已存在於另一個 vault 的 id 拒絕覆蓋——歸屬凍結，不因重跑管線而搬家。
    `mode="create"`：id 已存在即 `DuplicateRecord`（蒸餾新增用，防止 id 撞號時
    默默蓋掉另一條記憶）；`mode="update"`：id 不存在即 `NotFound`（校準回寫用）。
    新增的 concept 排在匯出順序最後；覆蓋不改變順序。
    """
    if mode not in UPSERT_MODES:
        raise ValueError(f"mode 必須是 {sorted(UPSERT_MODES)}，得到 {mode!r}")
    state, scope = _scope_columns(concept)
    data = _dumps(concept.to_dict())
    with transaction(conn):
        key = resolve_write(conn, vault, space=SPACE_DEV)
        existing = conn.execute(
            "SELECT vault, data FROM concepts WHERE id = ?", (concept.id,)
        ).fetchone()
        if existing is not None and existing["vault"] != key:
            raise DuplicateRecord(
                f"concept {concept.id!r} 已屬於 vault {existing['vault']!r}"
            )
        if existing is not None and mode == "create":
            raise DuplicateRecord(f"concept {concept.id!r} 已存在（mode=create）")
        if existing is None and mode == "update":
            raise NotFound(f"concept {concept.id!r} 不存在（mode=update）")
        if existing is not None:
            if existing["data"] == data:
                return CONCEPT_UNCHANGED
            conn.execute(
                """
                UPDATE concepts SET kind = ?, scope_state = ?, scope = ?, data = ?,
                                    updated = ?
                WHERE id = ?
                """,
                (concept.kind, state, scope, data, utc_now(), concept.id),
            )
            return CONCEPT_UPDATED
        conn.execute(
            """
            INSERT INTO concepts (id, vault, kind, scope_state, scope, data, updated,
                                  ord)
            VALUES (?, ?, ?, ?, ?, ?, ?,
                    (SELECT coalesce(max(ord), 0) + 1 FROM concepts))
            """,
            (concept.id, key, concept.kind, state, scope, data, utc_now()),
        )
        return CONCEPT_CREATED


def delete_concept(conn: sqlite3.Connection, vault: str, concept_id: str) -> bool:
    """刪除 vault 內的 concept（收斂淘汰輸家）；vault 內沒有這個 id 回 False。

    id 屬於另一個 vault 時拋 `DuplicateRecord`：不可跨 vault 刪，也不假裝「不存在」。
    """
    with transaction(conn):
        key = resolve_write(conn, vault, space=SPACE_DEV)
        owner = concept_owner(conn, concept_id)
        if owner is None:
            return False
        if owner != key:
            raise DuplicateRecord(f"concept {concept_id!r} 屬於 vault {owner!r}")
        conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
        return True


def export_concepts(conn: sqlite3.Connection, vault: str) -> tuple[list[Concept], int]:
    """依匯出順序（ord）取出全部 concept，給注入快照用。

    回傳 (concept 清單, 被排除的 scope 缺欄位筆數)。scope 缺欄位（MISSING）的
    concept 不匯出：spike scorer 以 `concept.get("scope")` 判斷，缺鍵會被當成
    None＝跨專案通用而放行到所有 repo（spike 的 scope 三態事故）。
    """
    scope = resolve_read(conn, vault, space=SPACE_DEV)
    clause, params = vault_clause(scope, "vault")
    rows = conn.execute(
        f"""
        SELECT data, scope_state FROM concepts WHERE {clause}
        ORDER BY ord, id
        """,
        params,
    ).fetchall()
    concepts = [
        Concept.from_dict(json.loads(r["data"]))
        for r in rows
        if r["scope_state"] != "missing"
    ]
    return concepts, len(rows) - len(concepts)


def get_concepts(
    conn: sqlite3.Connection, vault: str, ids: list[str] | tuple[str, ...]
) -> list[Concept]:
    scope = resolve_read(conn, vault, space=SPACE_DEV)
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
    scope = resolve_read(conn, vault, space=SPACE_DEV)
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
) -> bool:
    """寫入一筆注入 side-car；回傳是否實際寫入。

    冪等：同 vault 已有內容完全相同的紀錄（session_id、prompt_id、
    prompt_fingerprint、injected 全等）→ 視為重送，回傳 False、不寫入。
    `recorded` 不參與比對（重送時客戶端帶的時間可能不同）。
    代價：同一輪真的注入兩次完全相同的清單會被併成一筆——spike 的 hook
    本來就以 session 狀態避免同一條重複注入，不影響「哪些輪次被影響過」的判斷。
    """
    rec = normalize_utc(recorded) if recorded is not None else utc_now()
    data = _dumps(injection.to_dict())
    with transaction(conn):
        key = resolve_write(conn, vault, space=SPACE_DEV)
        existing = conn.execute(
            """
            SELECT 1 FROM injections
            WHERE vault = ? AND session_id = ? AND prompt_id IS ?
              AND prompt_fingerprint IS ? AND data = ?
            LIMIT 1
            """,
            (
                key,
                injection.session_id,
                injection.prompt_id,
                injection.prompt_fingerprint,
                data,
            ),
        ).fetchone()
        if existing is not None:
            return False
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
                data,
                rec,
            ),
        )
    return True


def list_injections(
    conn: sqlite3.Connection,
    vault: str,
    *,
    session_id: str | None = None,
    limit: int = 10000,
    cursor: int | None = None,
) -> tuple[list[Injection], int | None]:
    """依寫入順序分頁；回傳 (本頁, 下一頁 cursor（最後一筆的 seq）或 None)。"""
    scope = resolve_read(conn, vault, space=SPACE_DEV)
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
