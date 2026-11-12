"""提案領域層：核准、駁回、檢視、殼端（task_archive）的 register／claim／close。

單一交易原則：核准在同一個 `BEGIN IMMEDIATE` 內「條件式轉移 → 套用」，套用失敗
整段 rollback，提案維持 pending；不存在「提案結案了但沒套用」或「套用了但提案還開著」。
套用 note 變更時以 `Actor.system_apply` 呼叫 `notes.service`（`transaction` 可巢狀，
沿用外層交易），寫入來源因此轉為 ui，`updated_by` 仍記提案者。

依賴方向：本檔 → notes.service／storage.admin／storage.proposals；
notes.service 只依賴 `proposals.policy` 與 `storage.proposals`，不 import 本檔。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from lore_vault.notes import service as notes_service
from lore_vault.schema import Note
from lore_vault.storage import admin
from lore_vault.storage import proposals as store
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import NotFound
from lore_vault.storage.notes import get_note, get_notes
from lore_vault.storage.timeutil import format_utc, parse_utc, utc_now

from . import types as t
from .types import Actor, ProposalRow

# note_update 提案比對基準時用的欄位
_FIELDS = ("title", "body", "topics", "links", "supersedes")


@dataclass(frozen=True)
class Approved:
    """核准結果；`wake_*` 讓 API 層在交易提交後喚醒背景 worker。"""

    proposal: ProposalRow
    wake_worker: bool = False
    wake_documents: bool = False


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _note_fields(note: Note, names: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {name: _plain(getattr(note, name)) for name in names}


def _load(conn: sqlite3.Connection, proposal_id: str) -> ProposalRow:
    row = store.get(conn, proposal_id)
    if row is None:
        raise t.ProposalNotFound(f"找不到提案 {proposal_id!r}")
    return row


def _require_open_pending(row: ProposalRow, expected_rev: int) -> None:
    if row.rev != expected_rev or row.state != t.PENDING:
        if row.state == t.EXECUTING:
            raise t.ProposalExecuting(f"提案 {row.id} 正在由殼執行中")
        raise t.ProposalStateConflict(
            f"提案 {row.id} 已變動（目前狀態 {row.state}、rev {row.rev}），"
            "請重新整理後再處理",
            current_state=row.state,
            current_rev=row.rev,
        )


# ── 核准 ──


def approve(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expected_rev: int,
    reviewer: str,
    rebase_to: str | None = None,
) -> Approved:
    """UI 核准。刪除／note_update／link_change 在同一交易套用並結案為 applied；
    task_archive 只記同意（approved），由殼在下次 archive 時執行。"""
    with transaction(conn):
        store.expire_due(conn)
        row = _load(conn, proposal_id)
        _require_open_pending(row, expected_rev)
        if not _target_exists(conn, row):
            # 目標已不存在：提案轉 obsolete（要在交易內寫入，所以先記下、離開交易再拋）
            store.transition(
                conn,
                row.id,
                expect_state=t.PENDING,
                expect_rev=row.rev,
                to_state=t.OBSOLETE,
                via=t.VIA_SYSTEM,
                reason="target missing at approval",
            )
            result = None
        elif row.kind == t.KIND_TASK_ARCHIVE:
            if not store.transition(
                conn,
                row.id,
                expect_state=t.PENDING,
                expect_rev=row.rev,
                to_state=t.APPROVED,
                via=t.VIA_UI,
                by=reviewer,
            ):
                raise t.ProposalStateConflict("提案狀態剛被其他通道變更")
            result = Approved(_load(conn, row.id))
        else:
            result = _apply(conn, row, reviewer, rebase_to)
    if result is None:
        raise t.ProposalStateConflict(
            f"提案 {proposal_id} 的目標已不存在，已標為 obsolete",
            current_state=t.OBSOLETE,
        )
    return result


def _target_exists(conn: sqlite3.Connection, row: ProposalRow) -> bool:
    if row.target_type == t.TARGET_NOTE:
        return bool(get_notes(conn, row.vault, [row.target_id], space=row.space))
    if row.target_type == t.TARGET_DOCUMENT:
        try:
            admin.plan_document_deletion(
                conn, row.vault, row.target_id, space=row.space
            )
        except NotFound:
            return False
    return True


def _apply(
    conn: sqlite3.Connection, row: ProposalRow, reviewer: str, rebase_to: str | None
) -> Approved:
    """已持有寫入交易；失敗拋例外整段 rollback。"""
    if row.kind == t.KIND_NOTE_DELETE:
        _mark_applied(conn, row, reviewer)
        admin.delete_note(
            conn,
            row.vault,
            row.target_id,
            space=row.space,
            reason=f"review approve delete by {reviewer}",
        )
        return Approved(_load(conn, row.id))
    if row.kind == t.KIND_DOCUMENT_DELETE:
        _mark_applied(conn, row, reviewer)
        admin.delete_document(
            conn,
            row.vault,
            row.target_id,
            space=row.space,
            reason=f"review approve delete by {reviewer}",
        )
        return Approved(_load(conn, row.id), wake_documents=True)
    note = get_note(conn, row.vault, row.target_id, space=row.space)
    if row.kind == t.KIND_NOTE_UPDATE:
        changes = dict(row.payload.get("changes") or {})
        base = row.base_snapshot or {}
        drifted = [
            name for name in changes if _plain(getattr(note, name)) != base.get(name)
        ]
        if drifted and rebase_to != note.updated:
            raise t.ProposalStale(
                f"提案 {row.id} 被觸及的欄位在提出後已被改過（{drifted}）；"
                "請檢視三方差異後駁回，或明確選擇以目前版本為基準覆寫",
                base=base,
                current=_note_fields(note, list(changes)),
                proposed=changes,
                current_updated=note.updated,
                fields=drifted,
            )
        _mark_applied(conn, row, reviewer)
        kwargs = {k: changes[k] for k in _FIELDS if k in changes}
    else:  # link_change
        remove = set(row.payload.get("remove") or [])
        kwargs: dict[str, Any] = {}
        new_links = [i for i in note.links if i not in remove]
        if len(new_links) != len(note.links):
            kwargs["links"] = new_links
        move = row.payload.get("supersedes")
        if move and note.supersedes == move.get("from"):
            kwargs["supersedes"] = move.get("to")
        _mark_applied(conn, row, reviewer)
        if not kwargs:  # 已經是目標狀態：視為完成
            return Approved(_load(conn, row.id))
    result = notes_service.update(
        conn,
        row.vault,
        row.target_id,
        note.updated,
        space=row.space,
        actor=Actor.system_apply(row.proposer_principal, row.proposer_author),
        **kwargs,
    )
    conn.execute(
        "UPDATE review_proposals SET applied_updated = ? WHERE id = ?",
        (result.note.updated, row.id),
    )
    return Approved(_load(conn, row.id), wake_worker=True)


def _mark_applied(
    conn: sqlite3.Connection, row: ProposalRow, reviewer: str, *, via: str = t.VIA_UI
) -> None:
    if not store.transition(
        conn,
        row.id,
        expect_state=t.PENDING,
        expect_rev=row.rev,
        to_state=t.APPLIED,
        via=via,
        by=reviewer,
    ):
        raise t.ProposalStateConflict("提案狀態剛被其他通道變更")


def reject(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expected_rev: int,
    reviewer: str,
    reason: str,
) -> ProposalRow:
    """UI 駁回：只改提案列，目標（note、文件）完全不動。pending 與 approved（尚未
    由殼執行）的提案都可駁回。"""
    with transaction(conn):
        store.expire_due(conn)
        row = _load(conn, proposal_id)
        if row.state == t.EXECUTING:
            raise t.ProposalExecuting(f"提案 {row.id} 正在由殼執行中")
        if row.rev != expected_rev or row.state not in (t.PENDING, t.APPROVED):
            raise t.ProposalStateConflict(
                f"提案 {row.id} 已變動（目前狀態 {row.state}、rev {row.rev}）",
                current_state=row.state,
                current_rev=row.rev,
            )
        if not store.transition(
            conn,
            row.id,
            expect_state=row.state,
            expect_rev=row.rev,
            to_state=t.REJECTED,
            via=t.VIA_UI,
            by=reviewer,
            reason=reason,
        ):
            raise t.ProposalStateConflict("提案狀態剛被其他通道變更")
        return _load(conn, row.id)


# ── 檢視（UI 專用，含原文）──


def describe(conn: sqlite3.Connection, row: ProposalRow) -> dict[str, Any]:
    """UI 審核側欄：提案完整內容、目標目前狀態與連結標題。"""
    target: dict[str, Any] = {
        "type": row.target_type,
        "id": row.target_id,
        "title": None,
    }
    current: dict[str, Any] | None = None
    current_updated: str | None = None
    link_titles: dict[str, str] = {}
    if row.target_type == t.TARGET_NOTE:
        found = get_notes(conn, row.vault, [row.target_id], space=row.space)
        if found:
            note = found[0]
            target["title"] = note.title
            current_updated = note.updated
            if row.kind == t.KIND_NOTE_UPDATE:
                names = list((row.payload.get("changes") or {}).keys())
                current = _note_fields(note, names)
            elif row.kind == t.KIND_LINK_CHANGE:
                current = _note_fields(note, ["links", "supersedes"])
    elif row.target_type == t.TARGET_DOCUMENT:
        title = (row.payload.get("plan_summary") or {}).get("title")
        target["title"] = title
    ids: set[str] = set()
    if row.kind == t.KIND_LINK_CHANGE:
        ids.update(row.payload.get("remove") or [])
        move = row.payload.get("supersedes") or {}
        ids.update(i for i in (move.get("from"), move.get("to")) if i)
    if row.kind == t.KIND_NOTE_UPDATE:
        changes = row.payload.get("changes") or {}
        ids.update(changes.get("links") or [])
        if changes.get("supersedes"):
            ids.add(changes["supersedes"])
        ids.update((row.base_snapshot or {}).get("links") or [])
    if current:
        ids.update(current.get("links") or [])
    if ids:
        for note in get_notes(conn, row.vault, sorted(ids), space=row.space):
            link_titles[note.id] = note.title
    return {
        "proposal": row.to_admin(),
        "target": target,
        "current": current,
        "current_updated": current_updated,
        "link_titles": link_titles,
    }


# ── 殼端協調（只限 task_archive）──


def _require_archive(row: ProposalRow) -> None:
    if row.kind != t.KIND_TASK_ARCHIVE:
        raise t.ProposalKindForbidden(
            "這個端點只處理 task_archive 提案；其他類型只能由 UI 核准或駁回"
        )


def register_archive(
    conn: sqlite3.Connection,
    *,
    vault: str,
    name: str,
    payload: dict[str, Any],
    proposer_principal: str,
    proposer_author: str | None,
    change_version: int | None = None,
) -> store.Upserted:
    """殼的 archive 在授權閘門與規劃之後登記（upsert）：同內容回既有提案（可能已
    approved），內容不同則舊提案 superseded、新提案 pending。"""
    return store.upsert_open(
        conn,
        space="dev",
        vault=vault,
        kind=t.KIND_TASK_ARCHIVE,
        target_id=name,
        payload=payload,
        proposer_principal=proposer_principal,
        proposer_author=proposer_author,
        base_updated=None if change_version is None else str(change_version),
    )


def claim(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expect: str,
    plan_digest: str,
    now: str | None = None,
) -> tuple[ProposalRow, str]:
    """殼執行前取得租約：條件式 pending|approved → executing。兩個通道同時到達只有
    一個贏，輸的拿到 `ProposalResolved`。回傳 (提案, claim_id)。"""
    if expect not in (t.PENDING, t.APPROVED):
        raise ValueError("expect 只能是 pending 或 approved")
    stamp = now or utc_now()
    with transaction(conn):
        store.expire_due(conn, now=stamp)
        row = _load(conn, proposal_id)
        _require_archive(row)
        if row.payload.get("plan_digest") != plan_digest:
            raise t.ProposalStateConflict(
                "規劃已變動，提案內容與這次要執行的不符；請重新規劃",
                current_state=row.state,
            )
        claim_id = "claim_" + uuid.uuid4().hex
        expires = format_utc(
            parse_utc(stamp) + timedelta(seconds=t.CLAIM_LEASE_SECONDS)
        )
        if row.state != expect or not store.transition(
            conn,
            row.id,
            expect_state=expect,
            expect_rev=row.rev,
            to_state=t.EXECUTING,
            now=stamp,
            claim_id=claim_id,
            claim_expires_at=expires,
            claim_prev_state=expect,
        ):
            raise t.ProposalResolved(
                f"提案 {row.id} 已被另一個通道處理（目前狀態 {row.state}）",
                current_state=row.state,
            )
        return _load(conn, row.id), claim_id


def close_claim(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    claim_id: str,
    outcome: str,
    now: str | None = None,
) -> ProposalRow:
    """殼執行完畢：applied 結案；release 釋放租約、回到 claim 前的狀態。"""
    if outcome not in ("applied", "release"):
        raise ValueError("outcome 只能是 applied 或 release")
    stamp = now or utc_now()
    with transaction(conn):
        row = _load(conn, proposal_id)
        _require_archive(row)
        if row.state != t.EXECUTING or row.claim_id != claim_id:
            raise t.ProposalResolved(
                f"提案 {row.id} 的租約已失效（目前狀態 {row.state}）",
                current_state=row.state,
            )
        prev = row.claim_prev_state or t.PENDING
        if outcome == "applied":
            ok = store.transition(
                conn,
                row.id,
                expect_state=t.EXECUTING,
                expect_rev=row.rev,
                to_state=t.APPLIED,
                via=t.VIA_UI if prev == t.APPROVED else t.VIA_TOKEN,
                now=stamp,
            )
        else:
            ok = store.transition(
                conn,
                row.id,
                expect_state=t.EXECUTING,
                expect_rev=row.rev,
                to_state=prev,
                now=stamp,
                clear_claim=True,
            )
        if not ok:
            raise t.ProposalResolved("提案狀態剛被其他通道變更")
        return _load(conn, row.id)


def archive_status(
    conn: sqlite3.Connection, vault: str, name: str
) -> ProposalRow | None:
    store.expire_due(conn)
    return store.find_open(conn, vault, t.KIND_TASK_ARCHIVE, name)
