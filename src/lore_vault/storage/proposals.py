"""待審提案的儲存原語（schema v20 `review_proposals`）。

- 所有狀態轉移都是條件式 UPDATE（`WHERE id=? AND state=? [AND rev=?]`），
  rowcount≠1 即視為被別人先處理；沒有 check-then-act
- 同一 (vault, kind, target_id) 至多一筆開放提案由 partial unique index 保證，
  應用層的去重只是讓行為友善
- 每次狀態變更 rev+1（審核端的樂觀鎖）；去重命中是 no-op，不動 rev
- payload／base_snapshot 含筆記原文：只經 `ProposalRow.to_admin()`（UI 專用）外送，
  不寫日誌、不進快照
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from lore_vault.proposals import types as t
from lore_vault.proposals.types import ProposalRow

from .db import transaction
from .timeutil import format_utc, parse_utc, utc_now

_COLUMNS = (
    "id, space, vault, kind, target_type, target_id, payload, payload_digest, "
    "base_updated, base_snapshot, proposer_principal, proposer_author, state, rev, "
    "created_at, updated_at, expires_at, resolved_by, resolved_at, resolved_via, "
    "resolution_reason, superseded_by, claim_id, claim_expires_at, claim_prev_state, "
    "applied_updated"
)
_OPEN_SQL = "('pending', 'approved', 'executing')"


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_of(kind: str, target_id: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        _dumps({"kind": kind, "target_id": target_id, "payload": payload}).encode()
    ).hexdigest()


def row_to_proposal(row: sqlite3.Row) -> ProposalRow:
    data = {key: row[key] for key in row.keys()}  # noqa: SIM118
    data["payload"] = json.loads(data["payload"])
    snap = data["base_snapshot"]
    data["base_snapshot"] = None if snap is None else json.loads(snap)
    return ProposalRow(**data)


def get(conn: sqlite3.Connection, proposal_id: str) -> ProposalRow | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM review_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    return None if row is None else row_to_proposal(row)


def find_open(
    conn: sqlite3.Connection, vault: str, kind: str, target_id: str
) -> ProposalRow | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM review_proposals WHERE vault = ? AND kind = ? "
        f"AND target_id = ? AND state IN {_OPEN_SQL}",
        (vault, kind, target_id),
    ).fetchone()
    return None if row is None else row_to_proposal(row)


def open_for_targets(
    conn: sqlite3.Connection, vault: str, target_ids: Sequence[str]
) -> dict[str, list[ProposalRow]]:
    """vault 內這些目標的開放提案（依目標分組）。"""
    ids = list(target_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM review_proposals WHERE vault = ? "
        f"AND target_id IN ({marks}) AND state IN {_OPEN_SQL} "
        "ORDER BY created_at, id",
        (vault, *ids),
    ).fetchall()
    found: dict[str, list[ProposalRow]] = {}
    for row in rows:
        prop = row_to_proposal(row)
        found.setdefault(prop.target_id, []).append(prop)
    return found


def count_open(conn: sqlite3.Connection, vault: str) -> int:
    return int(
        conn.execute(
            f"SELECT count(*) FROM review_proposals WHERE vault = ? "
            f"AND state IN {_OPEN_SQL}",
            (vault,),
        ).fetchone()[0]
    )


def _expiry(now: str, ttl_days: float) -> str:
    return format_utc(parse_utc(now) + timedelta(days=ttl_days))


@dataclass(frozen=True)
class Upserted:
    proposal: ProposalRow
    deduped: bool
    superseded: ProposalRow | None = None


def transition(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expect_state: str,
    to_state: str,
    expect_rev: int | None = None,
    via: str | None = None,
    by: str | None = None,
    reason: str | None = None,
    now: str | None = None,
    superseded_by: str | None = None,
    applied_updated: str | None = None,
    claim_id: str | None = None,
    claim_expires_at: str | None = None,
    claim_prev_state: str | None = None,
    clear_claim: bool = False,
) -> bool:
    """條件式轉移：目前狀態（與 rev，若給）相符才寫入並 rev+1。回傳是否成功。"""
    stamp = now or utc_now()
    terminal = to_state in t.TERMINAL_STATES
    sets = ["state = ?", "rev = rev + 1", "updated_at = ?"]
    args: list[Any] = [to_state, stamp]
    if terminal or (to_state == t.APPROVED and via is not None):
        # by／via／reason 為 None 時不覆寫（保留核准者與原因）
        sets += [
            "resolved_by = COALESCE(?, resolved_by)",
            "resolved_at = ?",
            "resolved_via = COALESCE(?, resolved_via)",
            "resolution_reason = COALESCE(?, resolution_reason)",
        ]
        args += [by, stamp, via, reason]
    if terminal:
        # 終態不留筆記原文：只保留 digest、kind、target 等稽核欄位
        sets += ["payload = '{}'", "base_snapshot = NULL"]
    if superseded_by is not None:
        sets.append("superseded_by = ?")
        args.append(superseded_by)
    if applied_updated is not None:
        sets.append("applied_updated = ?")
        args.append(applied_updated)
    if clear_claim or terminal:
        sets += [
            "claim_id = NULL",
            "claim_expires_at = NULL",
            "claim_prev_state = NULL",
        ]
    if claim_id is not None:
        sets += ["claim_id = ?", "claim_expires_at = ?", "claim_prev_state = ?"]
        args += [claim_id, claim_expires_at, claim_prev_state]
    where = "id = ? AND state = ?"
    args += [proposal_id, expect_state]
    if expect_rev is not None:
        where += " AND rev = ?"
        args.append(expect_rev)
    cursor = conn.execute(
        f"UPDATE review_proposals SET {', '.join(sets)} WHERE {where}", args
    )
    return cursor.rowcount == 1


def update_payload(
    conn: sqlite3.Connection, proposal_id: str, payload: dict[str, Any]
) -> ProposalRow:
    """就地更新開放提案的 payload 與 digest（不動狀態與 rev）。task_archive 的同意
    綁 plan_digest，reason 等欄位改了不該洗掉使用者的核准。"""
    row = get(conn, proposal_id)
    assert row is not None
    conn.execute(
        "UPDATE review_proposals SET payload = ?, payload_digest = ? WHERE id = ?",
        (_dumps(payload), digest_of(row.kind, row.target_id, payload), proposal_id),
    )
    updated = get(conn, proposal_id)
    assert updated is not None
    return updated


def expire_due(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """惰性過期：pending／approved 超過 expires_at → expired；executing 的租約逾時
    → 回到 claim 前的狀態。回傳處理筆數。"""
    stamp = now or utc_now()
    count = 0
    with transaction(conn):
        due = conn.execute(
            "SELECT id, state, rev FROM review_proposals WHERE "
            "state IN ('pending', 'approved') AND expires_at <= ?",
            (stamp,),
        ).fetchall()
        for row in due:
            if transition(
                conn,
                row["id"],
                expect_state=row["state"],
                expect_rev=row["rev"],
                to_state=t.EXPIRED,
                via=t.VIA_SYSTEM,
                reason="expired",
                now=stamp,
            ):
                count += 1
        stuck = conn.execute(
            "SELECT id, rev, claim_prev_state FROM review_proposals WHERE "
            "state = 'executing' AND claim_expires_at IS NOT NULL "
            "AND claim_expires_at <= ?",
            (stamp,),
        ).fetchall()
        for row in stuck:
            if transition(
                conn,
                row["id"],
                expect_state=t.EXECUTING,
                expect_rev=row["rev"],
                to_state=row["claim_prev_state"] or t.PENDING,
                now=stamp,
                clear_claim=True,
            ):
                count += 1
    return count


def upsert_open(
    conn: sqlite3.Connection,
    *,
    space: str,
    vault: str,
    kind: str,
    target_id: str,
    payload: dict[str, Any],
    proposer_principal: str,
    proposer_author: str | None = None,
    base_updated: str | None = None,
    base_snapshot: dict[str, Any] | None = None,
    now: str | None = None,
    ttl_days: float = t.DEFAULT_TTL_DAYS,
    max_open: int = t.MAX_OPEN_PER_VAULT,
) -> Upserted:
    """建立開放提案；同目標同類型已有開放提案時：

    - 內容相同（payload_digest）→ 回傳既有的（`deduped=True`，不動 rev）
    - 內容不同 → 舊的轉 superseded（`superseded_by` 指向新提案）、新的 pending；
      舊的正被殼執行（executing）時拒絕（`ProposalExecuting`）
    每 vault 開放提案超過上限回 `ProposalQueueFull`。
    """
    stamp = now or utc_now()
    digest = digest_of(kind, target_id, payload)
    with transaction(conn):
        expire_due(conn, now=stamp)
        existing = find_open(conn, vault, kind, target_id)
        if existing is not None:
            if existing.payload_digest == digest:
                return Upserted(existing, True)
            if existing.state == t.EXECUTING:
                raise t.ProposalExecuting(
                    f"{kind} 提案 {existing.id} 正在執行中，暫時不能改寫"
                )
        elif count_open(conn, vault) >= max_open:
            raise t.ProposalQueueFull(
                f"vault {vault!r} 的開放提案已達上限 {max_open}；請先處理既有提案"
            )
        new_id = "prop_" + uuid.uuid4().hex
        if existing is not None and not transition(
            conn,
            existing.id,
            expect_state=existing.state,
            expect_rev=existing.rev,
            to_state=t.SUPERSEDED,
            via=t.VIA_SYSTEM,
            reason="superseded by newer proposal",
            now=stamp,
            superseded_by=new_id,
        ):
            raise t.ProposalStateConflict("提案狀態剛被其他通道變更，請重試")
        conn.execute(
            """
            INSERT INTO review_proposals (id, space, vault, kind, target_type,
                target_id, payload, payload_digest, base_updated, base_snapshot,
                proposer_principal, proposer_author, state, rev, created_at,
                updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
            """,
            (
                new_id,
                space,
                vault,
                kind,
                t.TARGET_OF_KIND[kind],
                target_id,
                _dumps(payload),
                digest,
                base_updated,
                None if base_snapshot is None else _dumps(base_snapshot),
                proposer_principal,
                proposer_author,
                stamp,
                stamp,
                _expiry(stamp, ttl_days),
            ),
        )
        created = get(conn, new_id)
        assert created is not None
        old = get(conn, existing.id) if existing is not None else None
        return Upserted(created, False, old)


def close_for_target(
    conn: sqlite3.Connection,
    vault: str,
    target_id: str,
    *,
    to_state: str,
    via: str,
    reason: str,
    kinds: Iterable[str] | None = None,
    by: str | None = None,
    now: str | None = None,
) -> int:
    """把目標上的開放提案（pending／approved）一律結案；executing 不動（殼正在執行）。
    UI 直接編輯／刪除（superseded、ui_direct）與目標消失（obsolete）共用。"""
    stamp = now or utc_now()
    where = "vault = ? AND target_id = ? AND state IN ('pending', 'approved')"
    args: list[Any] = [vault, target_id]
    if kinds is not None:
        kind_list = list(kinds)
        where += f" AND kind IN ({','.join('?' * len(kind_list))})"
        args += kind_list
    closed = 0
    with transaction(conn):
        for row in conn.execute(
            f"SELECT id, state, rev FROM review_proposals WHERE {where}", args
        ).fetchall():
            if transition(
                conn,
                row["id"],
                expect_state=row["state"],
                expect_rev=row["rev"],
                to_state=to_state,
                via=via,
                by=by,
                reason=reason,
                now=stamp,
            ):
                closed += 1
    return closed


def close_for_vault(
    conn: sqlite3.Connection, vault: str, *, reason: str, now: str | None = None
) -> int:
    """vault 刪除或換 space：該 vault 所有開放提案（含 executing）轉 obsolete。"""
    stamp = now or utc_now()
    closed = 0
    with transaction(conn):
        for row in conn.execute(
            f"SELECT id, state, rev FROM review_proposals WHERE vault = ? "
            f"AND state IN {_OPEN_SQL}",
            (vault,),
        ).fetchall():
            if transition(
                conn,
                row["id"],
                expect_state=row["state"],
                expect_rev=row["rev"],
                to_state=t.OBSOLETE,
                via=t.VIA_SYSTEM,
                reason=reason,
                now=stamp,
            ):
                closed += 1
    return closed


def queue_rows(
    conn: sqlite3.Connection,
    *,
    space: str,
    vaults: Sequence[str] | None,
    kinds: Sequence[str] | None,
    states: Sequence[str],
    limit: int,
    cursor: tuple[str, str] | None = None,
) -> list[ProposalRow]:
    """佇列的提案來源（keyset：(created_at, id) 由新到舊）。"""
    where = ["space = ?"]
    args: list[Any] = [space]
    if vaults is not None:
        where.append(f"vault IN ({','.join('?' * len(vaults))})")
        args += list(vaults)
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    where.append(f"state IN ({','.join('?' * len(states))})")
    args += list(states)
    if cursor is not None:
        where.append("(created_at, id) < (?, ?)")
        args += list(cursor)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM review_proposals WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [row_to_proposal(r) for r in rows]


def counts_by_kind(
    conn: sqlite3.Connection,
    *,
    space: str,
    vaults: Sequence[str] | None,
    states: Sequence[str] = t.QUEUE_DEFAULT_STATES,
) -> dict[str, int]:
    where = ["space = ?", f"state IN ({','.join('?' * len(states))})"]
    args: list[Any] = [space, *states]
    if vaults is not None:
        where.append(f"vault IN ({','.join('?' * len(vaults))})")
        args += list(vaults)
    rows = conn.execute(
        f"SELECT kind, count(*) AS n FROM review_proposals WHERE {' AND '.join(where)} "
        "GROUP BY kind",
        args,
    ).fetchall()
    return {r["kind"]: int(r["n"]) for r in rows}
