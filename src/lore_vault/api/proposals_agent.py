"""MCP 殼（bearer）與服務端協調任務封存（`task_archive`）提案的端點。

封存在殼端執行（多步寫入、不是單一服務端交易），所以用「同意＋租約」而不是服務端
套用：

- `POST /v1/proposal_register`：archive 在授權閘門與規劃之後登記（upsert）。同內容回既有
  提案（可能已被 UI 核准成 approved）；內容不同則舊提案 superseded、新提案 pending
- `POST /v1/proposal_claim`：執行前取得 5 分鐘租約（pending→executing，via 對話 token；
  approved→executing，via UI 核准）。兩個通道同時到達只有一個贏，輸的 409
  `proposal_resolved`
- `POST /v1/proposal_close`：執行完 `applied` 結案，或 `release` 釋放租約（回到 claim
  前的狀態）；租約逾時也會自動回復
- `POST /v1/proposal_status`：查某 change 目前的開放提案

只接受 `task_archive`：其他類型（刪除、note_update、link_change）只能由 UI session
核准或駁回，bearer 打這裡一律 403 `proposal_kind_forbidden`。回應只含提案的精簡形狀
（不含 payload）。殼是獨立程序、祕密獨立，服務端無法驗證殼持有有效 token：
claim(via=token) 是誠信聲明，與現狀等價；UI 核准才是服務端可驗證的同意。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.proposals.types import ProposalRow
from lore_vault.storage import proposals as storage_proposals
from lore_vault.storage.vaults import resolve_write

from .principals import principal_of
from .state import AppState

router = APIRouter(prefix="/v1")

TASK_SPACE = "dev"


def _state(request: Request) -> AppState:
    return request.app.state.lore


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space: str = TASK_SPACE
    vault: str


class RegisterRequest(_Req):
    name: str
    plan_digest: str
    reason: str | None = None
    allow_incomplete: bool = False
    change_version: int | None = None
    content_digest: str | None = None
    author: str | None = None


class ClaimRequest(_Req):
    id: str
    expect: str
    plan_digest: str


class CloseRequest(_Req):
    id: str
    claim_id: str
    outcome: str


class StatusRequest(_Req):
    name: str


def _vault(conn: Any, req: _Req) -> str:
    if req.space != TASK_SPACE:
        raise ValueError("任務層只屬於 dev space")
    return resolve_write(conn, req.vault, space=TASK_SPACE)


def _view(row: ProposalRow, **extra: Any) -> dict[str, Any]:
    return {**row.brief(), "rev": row.rev, **extra}


def _owned(conn: Any, vault: str, proposal_id: str) -> ProposalRow:
    row = storage_proposals.get(conn, proposal_id)
    if row is None or row.vault != vault or row.space != TASK_SPACE:
        raise t.ProposalNotFound(f"找不到提案 {proposal_id!r}")
    return row


@router.post("/proposal_register")
def proposal_register(request: Request, req: RegisterRequest) -> dict[str, Any]:
    principal = principal_of(request)
    with _state(request).connection() as conn:
        vault = _vault(conn, req)
        up = proposals_service.register_archive(
            conn,
            vault=vault,
            name=req.name,
            payload={
                "plan_digest": req.plan_digest,
                "reason": req.reason or "",
                "allow_incomplete": req.allow_incomplete,
                "change_version": req.change_version,
                "content_digest": req.content_digest,
            },
            proposer_principal=principal,
            proposer_author=req.author,
            change_version=req.change_version,
        )
    extra: dict[str, Any] = {"deduped": up.deduped}
    if up.superseded is not None:
        extra["superseded"] = up.superseded.id
    return {"proposal": _view(up.proposal, **extra)}


@router.post("/proposal_claim")
def proposal_claim(request: Request, req: ClaimRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        vault = _vault(conn, req)
        _owned(conn, vault, req.id)
        row, claim_id = proposals_service.claim(
            conn, req.id, expect=req.expect, plan_digest=req.plan_digest
        )
    return {
        "proposal": _view(row),
        "claim_id": claim_id,
        "lease_expires_at": row.claim_expires_at,
    }


@router.post("/proposal_close")
def proposal_close(request: Request, req: CloseRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        vault = _vault(conn, req)
        _owned(conn, vault, req.id)
        row = proposals_service.close_claim(
            conn, req.id, claim_id=req.claim_id, outcome=req.outcome
        )
    return {"proposal": _view(row)}


@router.post("/proposal_status")
def proposal_status(request: Request, req: StatusRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        vault = _vault(conn, req)
        row = proposals_service.archive_status(conn, vault, req.name)
    return {"proposal": None if row is None else _view(row)}
