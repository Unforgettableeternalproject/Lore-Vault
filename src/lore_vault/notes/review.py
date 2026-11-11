"""U.E.P 寫入審核（add-uep-interface）：狀態轉移與 uep 的限制。

狀態：`none`（非 uep 的一般 note）→ uep 在 lore 寫入／修改 → `pending` →
使用者核准 `verified`／駁回 `rejected`；uep 對 lore note 發刪除請求 → `pending_delete`
（note 照常可見，記下 `review_prev_state`），核准則真刪（留墓碑）、駁回則還原。

審核動作不動 `updated`（內容沒變，樂觀鎖與 embedding 都維持）。`reviewed_by`／
`reviewed_at`／`review_reason` 記「最近一次審核動作」：核准與駁回（含駁回刪除請求）
都會覆寫；uep 再次修改時清空。

呼叫端（HTTP 層）負責「只收 UI session」與 uep 的 space 限制；這裡只管狀態機。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lore_vault.schema import (
    REVIEW_NONE,
    REVIEW_PENDING,
    REVIEW_PENDING_DELETE,
    REVIEW_REJECTED,
    REVIEW_UNVERIFIED_STATES,
    REVIEW_VERIFIED,
    UEP_PRINCIPAL,
    Note,
)
from lore_vault.schema.chars import check_fields
from lore_vault.storage import admin
from lore_vault.storage.db import transaction
from lore_vault.storage.notes import get_note, set_note_review
from lore_vault.storage.timeutil import utc_now

# lore 以外 uep 的寫入直接生效（review_state 維持 none）
SPACE_LORE = "lore"
SPACE_DEV = "dev"
UEP_SPACES = (SPACE_DEV, SPACE_LORE)


class UepForbidden(Exception):
    """uep 的請求違反服務端限制（403）；`code` 為對外錯誤碼。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReviewStateConflict(Exception):
    """目前的審核狀態不允許這個動作（409 `review_state_conflict`）。"""

    def __init__(self, message: str, current: str, updated: str | None = None) -> None:
        super().__init__(message)
        self.current = current
        # 版本不符時附 note 目前的 updated，呼叫端重新讀取後再審
        self.updated = updated


def is_uep(principal: str) -> bool:
    return principal == UEP_PRINCIPAL


def is_review_space(principal: str, space: str) -> bool:
    """這次寫入是否進入審核：uep 在 lore。"""
    return is_uep(principal) and space == SPACE_LORE


def check_uep_space(principal: str, space: object) -> None:
    """uep 只能用 dev／lore；其他 space（含 personal）一律 403。space 缺漏
    （None）不在此處理，交給一般的 400 `space_required`。"""
    if is_uep(principal) and space is not None and space not in UEP_SPACES:
        raise UepForbidden(
            "uep_space_forbidden",
            f"uep 只能存取 {list(UEP_SPACES)} space，不可使用 {space!r}",
        )


def check_uep_owns(principal: str, note: Note) -> None:
    """uep 在 lore 只能修改自己（principal 為 uep）寫的 note。"""
    if is_uep(principal) and (
        note.principal != UEP_PRINCIPAL or note.updated_by_principal != UEP_PRINCIPAL
    ):
        raise UepForbidden(
            "uep_not_author",
            "uep 只能修改自己寫、且最後修改者也是 uep 的 note；要改他人的內容"
            "（或被使用者編輯過的 note）請另寫 note 並以 supersedes 指向原篇",
        )


def unverified(state: str) -> bool:
    return state in REVIEW_UNVERIFIED_STATES


def review_fields(note: Note) -> dict[str, Any]:
    return {
        "review_state": note.review_state,
        "reviewed_by": note.reviewed_by,
        "reviewed_at": note.reviewed_at,
        "review_reason": note.review_reason,
    }


def request_delete(
    conn: sqlite3.Connection, vault: str, note_id: str, *, space: str
) -> Note:
    """uep 在 lore 的刪除請求：記下原狀態、改為 pending_delete，不刪。"""
    with transaction(conn):
        note = get_note(conn, vault, note_id, space=space)
        assert_can_request_delete(note)
        moved = set_note_review(
            conn,
            vault,
            note_id,
            space=space,
            expect_state=note.review_state,
            review_state=REVIEW_PENDING_DELETE,
            reviewed_by=note.reviewed_by,
            reviewed_at=note.reviewed_at,
            review_reason=note.review_reason,
            review_prev_state=note.review_state,
        )
        if not moved:  # 交易內持有寫鎖，不該發生；保險
            raise ReviewStateConflict("審核狀態已變動", note.review_state)
        return get_note(conn, vault, note_id, space=space)


def assert_can_request_delete(note: Note) -> None:
    """已有刪除請求、或已被駁回（rejected 對讀者不可見，不可借刪除請求復活）的
    note 不能發刪除請求。"""
    if note.review_state == REVIEW_PENDING_DELETE:
        raise ReviewStateConflict(
            f"note {note.id!r} 已有待審核的刪除請求", note.review_state
        )
    if note.review_state == REVIEW_REJECTED:
        raise ReviewStateConflict(
            f"note {note.id!r} 已被駁回，不可再發刪除請求", note.review_state
        )


def _check_version(note: Note, expected_updated: str) -> None:
    if note.updated != expected_updated:
        raise ReviewStateConflict(
            f"note {note.id!r} 在你讀取後已被修改（目前 updated={note.updated}），"
            "請重新讀取確認內容後再審",
            note.review_state,
            note.updated,
        )


def approve(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    *,
    space: str,
    reviewer: str,
    expected_updated: str,
) -> dict[str, Any]:
    """pending → verified；pending_delete → 真刪（走既有刪除流程，留墓碑）。

    `expected_updated` 必須等於 note 目前的 `updated`（審核的是使用者看到的那一版，
    uep 在審核前改了內容就 409）。"""
    with transaction(conn):
        note = get_note(conn, vault, note_id, space=space)
        _check_version(note, expected_updated)
        now = utc_now()
        if note.review_state == REVIEW_PENDING:
            ok = set_note_review(
                conn,
                vault,
                note_id,
                space=space,
                expect_state=REVIEW_PENDING,
                review_state=REVIEW_VERIFIED,
                reviewed_by=reviewer,
                reviewed_at=now,
                review_reason=None,
                review_prev_state=None,
            )
            if not ok:
                raise ReviewStateConflict("審核狀態已變動", note.review_state)
            return {
                "id": note_id,
                "vault": note.vault,
                "deleted": False,
                **review_fields(get_note(conn, vault, note_id, space=space)),
            }
        if note.review_state == REVIEW_PENDING_DELETE:
            # 走既有的刪除（FTS、向量、墓碑同交易）。墓碑快照保留請求前的審核欄位
            # （還原時才不會殘留核准人）；核准人記在墓碑的 reason
            admin.delete_note(
                conn,
                vault,
                note_id,
                space=space,
                reason=f"review approve delete by {reviewer}",
            )
            return {
                "id": note_id,
                "vault": note.vault,
                "deleted": True,
                "review_state": None,
                "reviewed_by": reviewer,
                "reviewed_at": now,
                "review_reason": None,
            }
        raise ReviewStateConflict(
            f"note {note_id!r} 的審核狀態是 {note.review_state}，"
            "只有 pending／pending_delete 可核准",
            note.review_state,
        )


def reject(
    conn: sqlite3.Connection,
    vault: str,
    note_id: str,
    *,
    space: str,
    reviewer: str,
    reason: str,
    expected_updated: str,
) -> dict[str, Any]:
    """pending → rejected；pending_delete → 還原 review_prev_state（note 保留）。"""
    check_fields({"review_reason": reason})
    with transaction(conn):
        note = get_note(conn, vault, note_id, space=space)
        _check_version(note, expected_updated)
        now = utc_now()
        if note.review_state == REVIEW_PENDING:
            target, prev = REVIEW_REJECTED, None
        elif note.review_state == REVIEW_PENDING_DELETE:
            target, prev = note.review_prev_state or REVIEW_NONE, None
        else:
            raise ReviewStateConflict(
                f"note {note_id!r} 的審核狀態是 {note.review_state}，"
                "只有 pending／pending_delete 可駁回",
                note.review_state,
            )
        ok = set_note_review(
            conn,
            vault,
            note_id,
            space=space,
            expect_state=note.review_state,
            review_state=target,
            reviewed_by=reviewer,
            reviewed_at=now,
            review_reason=reason,
            review_prev_state=prev,
        )
        if not ok:
            raise ReviewStateConflict("審核狀態已變動", note.review_state)
        return {
            "id": note_id,
            "vault": note.vault,
            "deleted": False,
            **review_fields(get_note(conn, vault, note_id, space=space)),
        }
