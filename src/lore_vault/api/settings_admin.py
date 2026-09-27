"""執行期設定 API（D13）：讀取有效值、修改、還原預設，附稽核。

- `GET /v1/settings`：白名單每一項的有效值、預設值（設定檔／環境變數）、來源
  （`default`／`override`）、覆寫的時間與修改者；另附分類、不合法而被略過的覆寫、
  最近 20 筆稽核
- `POST /v1/settings_update`：`{"values": {鍵: 值}}`，整批驗證、全成或全不改
- `POST /v1/settings_reset`：`{"keys": [鍵]}`，刪除覆寫、回到預設值
- 修改與還原成功後回傳與 GET 相同的內容，外加 `changed`（這次寫入的稽核列）

**只允許 UI session**（管理者本人）：bearer 請求一律 403 `ui_session_required`。
bearer token 由所有 agent／hook 共用，`/mcp` 也以 bearer 在程序內轉發 `/v1`；開放 bearer
等於任何 agent 都能改隱私開關（episode 收料）與門檻，與 D13「預設關閉、自架者不會意外
集中對話原文」的目的相違。其他管理端點（`api.manage`）開放 bearer 是因為它們有兩段式
確認、動作可逆或有墓碑；設定沒有這層保護。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from lore_vault.runtime_settings import CATEGORIES, SPECS, base_value
from lore_vault.storage import settings_store
from lore_vault.storage.timeutil import utc_now

from .errors import UiSessionRequired
from .principals import AUTH_UI, auth_method_of, display_of, principal_of
from .state import AppState


def _require_ui(request: Request) -> None:
    if auth_method_of(request) != AUTH_UI:
        raise UiSessionRequired("服務設定只能由登入 UI 的管理者修改或查看")


# 認證方式在 body 驗證之前檢查：bearer 請求一律 403，拿不到 422 等任何細節
router = APIRouter(prefix="/v1", dependencies=[Depends(_require_ui)])


def _state(request: Request) -> AppState:
    return request.app.state.lore


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsUpdateRequest(_Req):
    values: dict[str, Any] = Field(min_length=1, max_length=len(SPECS))


class SettingsResetRequest(_Req):
    keys: list[str] = Field(min_length=1, max_length=len(SPECS))


def _snapshot(state: AppState) -> dict[str, Any]:
    base = state.runtime.base
    effective = state.runtime.current()
    with state.connection() as conn:
        values, rows, invalid = settings_store.effective_overrides(conn)
        audit = (
            settings_store.audit_log(conn) if settings_store.has_tables(conn) else []
        )
    items = []
    for spec in SPECS:
        row = rows.get(spec.key)
        items.append(
            {
                **spec.to_dict(),
                "value": base_value(effective, spec.key),
                "default": base_value(base, spec.key),
                "source": "override" if spec.key in values else "default",
                "override": (
                    {"updated": row.updated, "updated_by": row.updated_by}
                    if row is not None
                    else None
                ),
            }
        )
    return {
        "categories": [{"id": c, "label": label} for c, label in CATEGORIES],
        "items": items,
        "invalid_overrides": [
            {"key": row.key, "reason": reason, "updated": row.updated}
            for row, reason in invalid
        ],
        "audit": [entry.to_dict() for entry in audit],
    }


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _snapshot(_state(request))


def _change(
    request: Request,
    *,
    set_values: dict[str, Any] | None = None,
    reset_keys: list[str] | None = None,
) -> dict[str, Any]:
    state = _state(request)
    try:
        with state.connection() as conn:
            entries = settings_store.change(
                conn,
                state.runtime.base,
                set_values=set_values,
                reset_keys=reset_keys or (),
                principal=principal_of(request),
                display=display_of(request),
                now=utc_now(),
            )
    finally:
        # 交易已 commit（或整批失敗未寫入）；一律讓快取失效，下一次讀取即生效
        state.runtime.invalidate()
    return {**_snapshot(state), "changed": [e.to_dict() for e in entries]}


@router.post("/settings_update")
def update_settings(request: Request, req: SettingsUpdateRequest) -> dict[str, Any]:
    return _change(request, set_values=req.values)


@router.post("/settings_reset")
def reset_settings(request: Request, req: SettingsResetRequest) -> dict[str, Any]:
    return _change(request, reset_keys=req.keys)
