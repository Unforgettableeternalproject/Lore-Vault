"""審核頁統一佇列與提案審核端點（add-agent-review-proposals）。**只收 UI session**：
bearer 一律 403 `ui_session_required`（router 依賴，在 body 驗證之前檢查）。

| 端點 | 說明 |
|---|---|
| `POST /v1/review/queue` | `{space, vault, kinds?, states?, limit, cursor?}` → 分頁 |
| `POST /v1/review/counts` | `{space, vault}` → `{counts: {kind: n}}`（徽章用） |
| `POST /v1/review/proposal_get` | `{space, vault, id}` → 提案內容與目標目前值 |
| `POST /v1/review/proposal_approve` | `{space, vault, id, expected_rev, rebase_to?}` |
| `POST /v1/review/proposal_reject` | `{space, vault, id, expected_rev, reason}` |

佇列合併三個來源，依 (created_at, item_id) 由新到舊的 keyset 分頁（各來源各取
limit+1 再合併；游標為不透明字串）：

1. `review_proposals`（`source: proposal`，`item_id` 即提案 id）
2. UEP 既有 pending／pending_delete（`source: uep`，`kind: uep_review`，唯讀衍生；
   核准／駁回仍走 `/v1/review/approve|reject`）
3. 任務層待核准的 change（`source: task_auth`，`kind: task_auth`，衍生、不入表；
   核准仍走 `/v1/tasks_authorize`）

personal space 回空。提案端點的 `vault` 可為 `*`（space 內全部），給定時提案必須屬於
該 vault，範圍錯誤與不存在相同：404 `proposal_not_found`。
"""

from __future__ import annotations

import binascii
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from lore_vault.confirm import b64, unb64
from lore_vault.notes import InvalidCursor
from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.proposals.types import ProposalRow
from lore_vault.schema import SPACE_DEV
from lore_vault.storage import proposals as storage_proposals
from lore_vault.storage.notes import get_notes
from lore_vault.storage.vaults import resolve_read, validate_space, vault_clause

from . import tasks_admin
from .guards import ui_session_only
from .principals import principal_of
from .state import AppState

router = APIRouter(prefix="/v1/review", dependencies=[Depends(ui_session_only())])

KIND_UEP_REVIEW = "uep_review"
KIND_TASK_AUTH = "task_auth"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _state(request: Request) -> AppState:
    return request.app.state.lore


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 缺值交給儲存層拋 400（space_required／vault_required）
    space: str | None = None
    vault: str | None = None


class QueueRequest(_Req):
    kinds: list[str] | None = None
    states: list[str] | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = None


class CountsRequest(_Req):
    pass


class ProposalGetRequest(_Req):
    id: str


class ApproveProposalRequest(_Req):
    id: str
    expected_rev: int
    rebase_to: str | None = None


class RejectProposalRequest(_Req):
    id: str
    expected_rev: int
    reason: str


# ── 游標 ──


def _encode_cursor(created_at: str, item_id: str) -> str:
    return b64(json.dumps([created_at, item_id]).encode("utf-8"))


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    try:
        data = json.loads(unb64(cursor))
    except (ValueError, UnicodeError, binascii.Error):
        raise InvalidCursor(f"cursor 無法解析：{cursor!r}") from None
    if (
        not isinstance(data, list)
        or len(data) != 2
        or not all(isinstance(x, str) for x in data)
    ):
        raise InvalidCursor(f"cursor 格式錯誤：{cursor!r}")
    return data[0], data[1]


# ── 範圍 ──


def _scope(conn: sqlite3.Connection, req: _Req) -> tuple[str, list[str] | None, Any]:
    """(space, vault key 清單或 None＝space 內全部, VaultScope)。"""
    space = validate_space(req.space)
    scope = resolve_read(conn, req.vault, space=space)
    return space, (None if scope.is_all else [scope.key]), scope


def _owned(conn: sqlite3.Connection, req: _Req, proposal_id: str) -> ProposalRow:
    space, vaults, _ = _scope(conn, req)
    row = storage_proposals.get(conn, proposal_id)
    if (
        row is None
        or row.space != space
        or (vaults is not None and row.vault not in vaults)
    ):
        raise t.ProposalNotFound(f"找不到提案 {proposal_id!r}")
    return row


# ── 佇列項目 ──


def _titles(conn: sqlite3.Connection, rows: list[ProposalRow]) -> dict[str, str]:
    """note 目標的目前標題（依 (vault, id)）。"""
    by_vault: dict[str, list[str]] = {}
    for row in rows:
        if row.target_type == t.TARGET_NOTE:
            by_vault.setdefault(row.vault, []).append(row.target_id)
    found: dict[str, str] = {}
    for vault, ids in by_vault.items():
        space = next(r.space for r in rows if r.vault == vault)
        for note in get_notes(conn, vault, ids, space=space):
            found[f"{vault}\0{note.id}"] = note.title
    return found


def _stale(conn: sqlite3.Connection, row: ProposalRow) -> bool:
    """note_update：被觸及欄位的目前值已不同於提出時的基準（核准得 proposal_stale）。"""
    if row.kind != t.KIND_NOTE_UPDATE or row.state != t.PENDING:
        return False
    found = get_notes(conn, row.vault, [row.target_id], space=row.space)
    if not found:
        return False
    base = row.base_snapshot or {}
    for name in row.payload.get("changes") or {}:
        value = getattr(found[0], name)
        if (list(value) if isinstance(value, tuple) else value) != base.get(name):
            return True
    return False


def _summary(row: ProposalRow, title: str | None) -> str:
    summary = row.payload.get("plan_summary") or {}
    shown = title or summary.get("title") or row.target_id
    if row.kind == t.KIND_NOTE_DELETE:
        return f"刪除筆記「{shown}」"
    if row.kind == t.KIND_DOCUMENT_DELETE:
        return f"刪除文件「{shown}」"
    if row.kind == t.KIND_NOTE_UPDATE:
        fields = "、".join((row.payload.get("changes") or {}).keys())
        return f"修改筆記「{shown}」（{fields}）"
    if row.kind == t.KIND_LINK_CHANGE:
        parts = []
        if row.payload.get("remove"):
            parts.append(f"移除 {len(row.payload['remove'])} 個連結")
        if row.payload.get("supersedes"):
            parts.append("變更取代關係")
        return f"筆記「{shown}」：{'、'.join(parts)}"
    return f"封存 change {row.target_id}"


def _proposal_item(
    conn: sqlite3.Connection, row: ProposalRow, titles: dict[str, str]
) -> dict[str, Any]:
    title = titles.get(f"{row.vault}\0{row.target_id}")
    if row.target_type == t.TARGET_DOCUMENT:
        title = (row.payload.get("plan_summary") or {}).get("title")
    elif row.target_type == t.TARGET_TASK_CHANGE:
        title = row.target_id
    return {
        "item_id": row.id,
        "source": "proposal",
        "kind": row.kind,
        "vault": row.vault,
        "target": {"type": row.target_type, "id": row.target_id, "title": title},
        "summary": _summary(row, title),
        "proposer": {
            "principal": row.proposer_principal,
            "author": row.proposer_author,
        },
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "state": row.state,
        "rev": row.rev,
        "stale": _stale(conn, row),
        "deep_link": None,
    }


def _uep_items(
    conn: sqlite3.Connection,
    scope: Any,
    limit: int,
    cursor: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    clause, params = vault_clause(scope, "n.vault")
    where = [
        clause,
        "n.review_state IN ('pending', 'pending_delete')",
        "n.vault IN (SELECT key FROM vaults WHERE space = 'lore')",
    ]
    args: list[Any] = [*params]
    if cursor is not None:
        where.append("(n.updated, 'uep:' || n.id) < (?, ?)")
        args += list(cursor)
    rows = conn.execute(
        f"""
        SELECT n.id, n.vault, n.title, n.updated, n.review_state,
               n.updated_by, n.updated_by_principal
        FROM notes n WHERE {" AND ".join(where)}
        ORDER BY n.updated DESC, n.id DESC LIMIT ?
        """,
        (*args, limit),
    ).fetchall()
    return [
        {
            "item_id": "uep:" + r["id"],
            "source": "uep",
            "kind": KIND_UEP_REVIEW,
            "vault": r["vault"],
            "target": {"type": "note", "id": r["id"], "title": r["title"]},
            "summary": (
                f"UEP 刪除請求：「{r['title']}」"
                if r["review_state"] == "pending_delete"
                else f"UEP 寫入待審：「{r['title']}」"
            ),
            "proposer": {
                "principal": r["updated_by_principal"] or "uep",
                "author": r["updated_by"],
            },
            "created_at": r["updated"],
            "expires_at": None,
            "state": r["review_state"],
            "rev": 0,
            "stale": False,
            "deep_link": None,
        }
        for r in rows
    ]


def _task_auth_items(
    conn: sqlite3.Connection,
    vaults: list[str] | None,
    cursor: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    items = []
    for found in tasks_admin.pending_authorizations(
        conn, vault=None if vaults is None else vaults[0]
    ):
        item_id = f"task_auth:{found['vault']}:{found['change']}"
        if cursor is not None and (found["updated"], item_id) >= tuple(cursor):
            continue
        items.append(
            {
                "item_id": item_id,
                "source": "task_auth",
                "kind": KIND_TASK_AUTH,
                "vault": found["vault"],
                "target": {
                    "type": "task_change",
                    "id": found["change"],
                    "title": found["change"],
                },
                "summary": f"change {found['change']} 待使用者核准",
                "proposer": {"principal": "agent", "author": None},
                "created_at": found["updated"],
                "expires_at": None,
                "state": "pending",
                "rev": 0,
                "stale": False,
                "deep_link": None,
            }
        )
    items.sort(key=lambda i: (i["created_at"], i["item_id"]), reverse=True)
    return items


def _wanted(kinds: list[str] | None, kind: str) -> bool:
    return not kinds or kind in kinds


@router.post("/queue")
def queue(request: Request, req: QueueRequest) -> dict[str, Any]:
    cursor = _decode_cursor(req.cursor)
    states = req.states or list(t.QUEUE_DEFAULT_STATES)
    bad = [s for s in states if s not in t.STATES]
    if bad:
        raise ValueError(f"states 含不合法的值：{bad}")
    allowed = (*t.KINDS, KIND_UEP_REVIEW, KIND_TASK_AUTH)
    unknown = [k for k in (req.kinds or []) if k not in allowed]
    if unknown:
        raise ValueError(f"kinds 含不合法的值：{unknown}")
    with _state(request).connection() as conn:
        space, vaults, scope = _scope(conn, req)
        if space not in t.SPACES:  # personal 不納入
            return {"items": [], "next_cursor": None, "has_more": False}
        storage_proposals.expire_due(conn)
        proposal_kinds = [k for k in (req.kinds or t.KINDS) if k in t.KINDS]
        rows: list[ProposalRow] = []
        if proposal_kinds:
            rows = storage_proposals.queue_rows(
                conn,
                space=space,
                vaults=vaults,
                kinds=proposal_kinds,
                states=states,
                limit=req.limit + 1,
                cursor=cursor,
            )
        titles = _titles(conn, rows)
        items = [_proposal_item(conn, r, titles) for r in rows]
        if space == "lore" and _wanted(req.kinds, KIND_UEP_REVIEW):
            items += _uep_items(conn, scope, req.limit + 1, cursor)
        if space == SPACE_DEV and _wanted(req.kinds, KIND_TASK_AUTH):
            items += _task_auth_items(conn, vaults, cursor)
    items.sort(key=lambda i: (i["created_at"], i["item_id"]), reverse=True)
    has_more = len(items) > req.limit
    page = items[: req.limit]
    return {
        "items": page,
        "next_cursor": (
            _encode_cursor(page[-1]["created_at"], page[-1]["item_id"])
            if has_more
            else None
        ),
        "has_more": has_more,
    }


@router.post("/counts")
def counts(request: Request, req: CountsRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        space, vaults, scope = _scope(conn, req)
        if space not in t.SPACES:
            return {"counts": {}}
        storage_proposals.expire_due(conn)
        found: dict[str, int] = dict(
            storage_proposals.counts_by_kind(conn, space=space, vaults=vaults)
        )
        if space == "lore":
            n = len(_uep_items(conn, scope, 10_000, None))
            if n:
                found[KIND_UEP_REVIEW] = n
        if space == SPACE_DEV:
            n = len(_task_auth_items(conn, vaults, None))
            if n:
                found[KIND_TASK_AUTH] = n
    return {"counts": found}


@router.post("/proposal_get")
def proposal_get(request: Request, req: ProposalGetRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        storage_proposals.expire_due(conn)
        row = _owned(conn, req, req.id)
        return proposals_service.describe(conn, row)


@router.post("/proposal_approve")
def proposal_approve(request: Request, req: ApproveProposalRequest) -> dict[str, Any]:
    state = _state(request)
    with state.connection() as conn:
        _owned(conn, req, req.id)
        approved = proposals_service.approve(
            conn,
            req.id,
            expected_rev=req.expected_rev,
            reviewer=principal_of(request),
            rebase_to=req.rebase_to,
        )
    if approved.wake_worker:
        state.wake_worker()
    if approved.wake_documents:
        state.wake_documents()
    return {"proposal": approved.proposal.to_admin()}


@router.post("/proposal_reject")
def proposal_reject(request: Request, req: RejectProposalRequest) -> dict[str, Any]:
    reason = req.reason.strip()
    if not reason:
        raise ValueError("reason 不可為空")
    with _state(request).connection() as conn:
        _owned(conn, req, req.id)
        row = proposals_service.reject(
            conn,
            req.id,
            expected_rev=req.expected_rev,
            reviewer=principal_of(request),
            reason=reason,
        )
    return {"proposal": row.to_admin()}
