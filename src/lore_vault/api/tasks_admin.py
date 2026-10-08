"""任務層授權紀錄的寫入端點（TASK_LAYER_MCP §3.3、MCP-T5）。

`POST /v1/tasks_authorize`：`{"space"?: "dev", "vault": str, "change": str}`。
艾斯維爾本人在 UI 任務頁按「核准」，服務端讀當下的 `task-change:<name>`，寫入
`task-authorization:<name>`；MCP 的 `tasks(action="archive")` 只認這份紀錄。

- **只允許 UI session**（cookie＋`X-Lore-Vault-UI`）：bearer 一律 403
  `ui_session_required`，在 body 驗證之前檢查（比照 `api.settings_admin`）。bearer
  token 由所有 agent／hook 共用，`/mcp` 也以 bearer 轉發 `/v1`；開放 bearer 等於讓 AI
  自己核准自己
- `authorized_by`（UI 登入者顯示名稱，沒有則用 principal）、`principal`
  （`{"kind": "ui_session", "name": principal}`）、`authorized_at`、`change_version`
  全部由服務端填；body 帶這些或任何未知欄位 422
- change 必須存在（否則 404 `not_found`）、內容是 schema 1 的 JSON（否則 409
  `change_invalid`）、`state == "active"`（否則 409 `change_not_active`）、
  `meta.requires_authorization` 為 true（否則 409 `authorization_not_required`）
- 已有同一版本的紀錄時不重寫（保留原核准時間），回 `created: false`；版本不同（核准後
  又 edit 過）就覆寫成目前版本
- `tasks.remote_sync` 關閉時 403 `tasks_remote_sync_disabled`（這是 `task-` 前綴的寫入）
- 稽核：紀錄本身即「誰、何時、核准哪個版本」；另寫一行 `lore_vault.api.tasks` log
- 不提供撤銷：核准後任何 edit 都會讓紀錄失效（`authorization_stale`），
  要撤回就再改一次 change

`/v1/blob_put` 對 `task-authorization:` 前綴一律 403 `authorization_write_forbidden`
（不論認證方式），這裡是唯一的寫入路徑。

核心不 import 任務層（TASK_LAYER_MCP §2）：下列 key 前綴、schema、狀態與 principal 種類
是 `lore_vault.tasks.remote_store` 模組 docstring 定義的服務端資料格式，兩邊必須一致。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.errors import NotFound
from lore_vault.storage.vaults import resolve_write

from .errors import (
    TaskAuthorizationRejected,
    TasksRemoteSyncDisabled,
    UiSessionRequired,
)
from .principals import AUTH_UI, auth_method_of, display_of, principal_of
from .state import AppState

log = logging.getLogger("lore_vault.api.tasks")

# 與 `lore_vault.tasks.remote_store` 的資料格式一致（見模組 docstring）
CHANGE_PREFIX = "task-change:"
AUTHORIZATION_PREFIX = "task-authorization:"
SCHEMA = 1
STATE_ACTIVE = "active"
PRINCIPAL_UI = "ui_session"
TASK_SPACE = "dev"
MIME = "application/json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _require_ui(request: Request) -> None:
    if auth_method_of(request) != AUTH_UI:
        raise UiSessionRequired("任務核准只能由登入 UI 的使用者本人操作")


# 認證方式在 body 驗證之前檢查：bearer 請求一律 403，拿不到 422 等任何細節
router = APIRouter(prefix="/v1", dependencies=[Depends(_require_ui)])


def _state(request: Request) -> AppState:
    return request.app.state.lore


class TasksAuthorizeRequest(BaseModel):
    # 授權人、principal、時間、版本都由服務端填；帶了（或任何未知欄位）一律 422
    model_config = ConfigDict(extra="forbid")

    space: str = TASK_SPACE
    vault: str
    change: str


def _now() -> str:
    # 授權紀錄的時間格式到秒（`YYYY-MM-DDTHH:MM:SSZ`），不是 utc_now 的毫秒格式
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _change_doc(blob: storage_sidecar.SidecarBlob, name: str) -> dict[str, Any]:
    try:
        data = json.loads(blob.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise TaskAuthorizationRejected(
            "change_invalid", f"change {name} 的服務端內容不是 UTF-8 JSON"
        ) from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise TaskAuthorizationRejected(
            "change_invalid", f"change {name} 的服務端內容 schema 不是 {SCHEMA}"
        )
    if data.get("name") != name:
        raise TaskAuthorizationRejected(
            "change_invalid", f"change {name} 的服務端內容名稱不符"
        )
    return data


def _existing(
    conn: Any, vault: str, name: str, version: int
) -> tuple[dict[str, Any], int] | None:
    """已有同一版本、由 UI 核准的紀錄時回 (紀錄, 側載版本)；否則 None（要寫新的）。"""
    try:
        blob = storage_sidecar.get(
            conn, vault, AUTHORIZATION_PREFIX + name, space=TASK_SPACE
        )
    except NotFound:
        return None
    try:
        data = json.loads(blob.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    principal = data.get("principal") if isinstance(data, dict) else None
    if (
        isinstance(data, dict)
        and data.get("schema") == SCHEMA
        and data.get("change") == name
        and data.get("change_version") == version
        and isinstance(principal, dict)
        and principal.get("kind") == PRINCIPAL_UI
    ):
        return data, blob.version
    return None


@router.post("/tasks_authorize")
def tasks_authorize(request: Request, req: TasksAuthorizeRequest) -> dict[str, Any]:
    """核准一個 `requires_authorization` 的 change 的目前版本；回
    `{"record": 授權紀錄, "version": 紀錄的側載版本, "created": bool}`。"""
    state = _state(request)
    if req.space != TASK_SPACE:
        raise ValueError("任務層只屬於 dev space")
    name = req.change
    if not NAME_RE.match(name) or name == "archive":
        raise ValueError(
            f"change 名稱 {name!r} 只能用小寫英數與 -（kebab-case），且不可為 archive"
        )
    if not state.runtime.current().tasks.remote_sync:
        raise TasksRemoteSyncDisabled(
            "服務未開啟任務層遠端同步（設定 tasks.remote_sync）；無法寫入授權紀錄"
        )
    principal = principal_of(request)
    display = display_of(request) or principal
    with state.connection() as conn:
        vault = resolve_write(conn, req.vault, space=TASK_SPACE)
        change_blob = storage_sidecar.get(
            conn, vault, CHANGE_PREFIX + name, space=TASK_SPACE
        )
        doc = _change_doc(change_blob, name)
        if doc.get("state") != STATE_ACTIVE:
            raise TaskAuthorizationRejected(
                "change_not_active",
                f"change {name} 的狀態是 {doc.get('state')!r}，只有進行中（active）"
                "的 change 可以核准",
            )
        meta = doc.get("meta")
        if not (isinstance(meta, dict) and meta.get("requires_authorization") is True):
            raise TaskAuthorizationRejected(
                "authorization_not_required",
                f"change {name} 沒有標記 requires_authorization，不需要核准",
            )
        version = change_blob.version
        found = _existing(conn, vault, name, version)
        if found is not None:
            record, record_version = found
            return {"record": record, "version": record_version, "created": False}
        record = {
            "schema": SCHEMA,
            "vault": vault,
            "change": name,
            "change_version": version,
            "authorized_by": display,
            "authorized_at": _now(),
            "principal": {"kind": PRINCIPAL_UI, "name": principal},
        }
        content = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        written = storage_sidecar.put(
            conn,
            vault,
            AUTHORIZATION_PREFIX + name,
            content,
            space=TASK_SPACE,
            mime=MIME,
        )
    log.info(
        "任務核准：vault=%s change=%s change_version=%d principal=%s display=%s",
        vault,
        name,
        version,
        principal,
        display,
    )
    return {"record": record, "version": written.version, "created": True}
