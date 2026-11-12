"""審核端點（add-uep-interface）：approve／reject，只收 UI session。

- bearer 一律 403 `ui_session_required`（router 層依賴，在 body 驗證之前檢查）
- body 帶 `space`、`vault`、`id`（reject 另帶 `reason`），與其他寫入端點一致；
  note 必須在該 space 的該 vault 內（範圍錯誤與不存在相同：404）
- `pending` → approve＝verified、reject＝rejected；`pending_delete` → approve＝真刪
  （走既有刪除流程，留墓碑）、reject＝還原 `review_prev_state`；其他狀態 409
  `review_state_conflict`
- 審核人取自 UI session 的 principal（不可由請求指定）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from lore_vault.notes import review as review_service

from .guards import ui_only_route, ui_session_only
from .principals import principal_of
from .state import AppState

_require_ui = ui_session_only()


router = APIRouter(
    prefix="/v1/review",
    dependencies=[Depends(_require_ui)],
    route_class=ui_only_route(),
)


def _state(request: Request) -> AppState:
    return request.app.state.lore


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # vault／space 缺值交給儲存層拋 400（vault_required／space_required）
    space: str | None = None
    vault: str | None = None
    id: str
    # 必填：使用者審核的那一版（get 回傳的 updated）；不符回 409 review_state_conflict
    expected_updated: str


class ApproveRequest(_Req):
    pass


class RejectRequest(_Req):
    reason: str


@router.post("/approve")
def approve(request: Request, req: ApproveRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        return review_service.approve(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
            reviewer=principal_of(request),
            expected_updated=req.expected_updated,
        )


@router.post("/reject")
def reject(request: Request, req: RejectRequest) -> dict[str, Any]:
    reason = req.reason.strip()
    if not reason:
        raise ValueError("reason 不可為空")
    with _state(request).connection() as conn:
        return review_service.reject(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
            reviewer=principal_of(request),
            reason=reason,
            expected_updated=req.expected_updated,
        )
