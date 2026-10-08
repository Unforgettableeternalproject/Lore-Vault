"""任務層授權紀錄的寫入端點與 `task-change:` 寫入守衛（TASK_LAYER_MCP §3.3、MCP-T5）。

`POST /v1/tasks_authorize`：`{"space"?: "dev", "vault": str, "change": str}`。
艾斯維爾本人在 UI 任務頁按「核准」，服務端讀當下的 `task-change:<name>`，寫入
`task-authorization:<name>`；MCP／CLI 的 archive 與段二落地只認這份紀錄。

- **只允許 UI session**（cookie＋`X-Lore-Vault-UI`）：bearer 一律 403
  `ui_session_required`，在 body 驗證之前檢查（比照 `api.settings_admin`）。bearer
  token 由所有 agent／hook 共用，`/mcp` 也以 bearer 轉發 `/v1`；開放 bearer 等於讓 AI
  自己核准自己
- `authorized_by`（UI 登入者顯示名稱，沒有則用 principal）、`principal`
  （`{"kind": "ui_session", "name": principal}`）、`authorized_at`、`change_version`、
  `content_digest` 全部由服務端填；body 帶這些或任何未知欄位 422
- `content_digest`：核准當下 change 的內容雜湊（`task_format.authorization_digest`，
  排除 archive 簿記）。是否失效一律以它比對；`change_version` 只供顯示
- change 必須存在（否則 404 `not_found`）、內容是 schema 1 的 JSON（否則 409
  `change_invalid`）、`state == "active"`（否則 409 `change_not_active`）、
  `meta.requires_authorization` 為 true（否則 409 `authorization_not_required`）
- 已有同一內容雜湊的紀錄時不重寫（保留原核准時間），回 `created: false`；雜湊不同
  （核准後又 edit 過）就覆寫成目前內容
- `tasks.remote_sync` 關閉時 403 `tasks_remote_sync_disabled`（這是 `task-` 前綴的寫入）
- 稽核：紀錄本身即「誰、何時、核准哪份內容」；另寫一行 `lore_vault.api.tasks` log
- 不提供撤銷：核准後任何內容修改都會讓紀錄失效（`authorization_stale`），
  要撤回就再改一次 change

`POST /v1/tasks_authorization_status`（同樣只收 UI session，唯讀）：回服務端 change 的
版本／狀態／內容雜湊與授權紀錄、`approved`（紀錄雜湊等於目前內容雜湊），給 UI 判斷
核准是否過期（雜湊只在服務端算，前端不重做一份）。

`/v1/blob_put` 對 `task-authorization:` 前綴一律 403 `authorization_write_forbidden`
（不論認證方式），這裡是唯一的寫入路徑；寫 `task-change:` 時經 `guard_change_write`
（規則見該函式）。

核心不 import 任務層（TASK_LAYER_MCP §2）：key 前綴、schema、狀態、principal 種類與
內容雜湊定義在 `api.task_format`，任務層 `remote_store` 直接 import 同一份。
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

from . import task_format as tf
from .errors import (
    TaskAuthorizationRejected,
    TaskChangeWriteForbidden,
    TasksRemoteSyncDisabled,
    UiSessionRequired,
)
from .principals import AUTH_UI, auth_method_of, display_of, principal_of
from .state import AppState

log = logging.getLogger("lore_vault.api.tasks")

CHANGE_PREFIX = tf.CHANGE_PREFIX
AUTHORIZATION_PREFIX = tf.AUTHORIZATION_PREFIX
SCHEMA = tf.SCHEMA
STATE_ACTIVE = tf.STATE_ACTIVE
PRINCIPAL_UI = tf.PRINCIPAL_UI
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


def _json_dict(content: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _change_doc(content: bytes, name: str) -> dict[str, Any]:
    """change 文件的最小格式檢查（守衛與核准要用的欄位）；
    不合格 409 `change_invalid`。"""
    data = _json_dict(content)
    if data is None:
        raise TaskAuthorizationRejected(
            "change_invalid", f"change {name} 的內容不是 UTF-8 JSON 物件"
        )
    if data.get("schema") != SCHEMA:
        raise TaskAuthorizationRejected(
            "change_invalid", f"change {name} 的內容 schema 不是 {SCHEMA}"
        )
    if data.get("name") != name:
        raise TaskAuthorizationRejected("change_invalid", f"change {name} 的名稱不符")
    if not isinstance(data.get("meta"), dict):
        raise TaskAuthorizationRejected("change_invalid", f"change {name} 缺少 meta")
    if data.get("state") not in tf.STATES:
        raise TaskAuthorizationRejected(
            "change_invalid",
            f"change {name} 的 state {data.get('state')!r} 不是 {'／'.join(tf.STATES)}",
        )
    return data


def valid_record(data: dict[str, Any] | None, name: str) -> bool:
    """授權紀錄是否為 UI 核准、格式完整（含 `content_digest`）。"""
    if data is None or data.get("schema") != SCHEMA or data.get("change") != name:
        return False
    principal = data.get("principal")
    by = data.get("authorized_by")
    return (
        isinstance(principal, dict)
        and principal.get("kind") == PRINCIPAL_UI
        and isinstance(by, str)
        and bool(by.strip())
        and tf.is_digest(data.get("content_digest"))
    )


def _record(
    conn: Any, vault: str, name: str, space: object = TASK_SPACE
) -> tuple[dict[str, Any], int] | None:
    """目前的 UI 核准紀錄 (內容, 側載版本)；不存在或格式不合格回 None。"""
    try:
        blob = storage_sidecar.get(
            conn, vault, AUTHORIZATION_PREFIX + name, space=space
        )
    except NotFound:
        return None
    data = _json_dict(blob.content)
    if data is None or not valid_record(data, name):
        return None
    return data, blob.version


# ── `/v1/blob_put` 的 task-change: 守衛 ──────────────────────────────


def guard_change_write(
    conn: Any,
    vault: str | None,
    key: str,
    content: bytes,
    *,
    space: object,
    expected_version: int | None,
) -> int | None:
    """`task-change:<name>` 寫入前的授權守衛；回傳寫入時必須使用的 `expected_version`。

    不論認證方式（bearer 由所有 agent 共用，AI 可直接打 HTTP）：

    1. 服務端**現有**內容 `meta.requires_authorization` 為 true 時：
       - 新內容必須是 schema 1、名稱相符、有 meta、state 合法的 JSON，否則 409
         `change_invalid`
       - 新內容必須仍為 true，否則 403 `authorization_downgrade_forbidden`
       - 新內容 `state` 不是 active，或 archive 簿記（`task_format.bookkeeping`：meta 的
         notes／note_id／authorization／mirror_* 等加 `apply`）與現有內容不同時，必須有
         UI 核准紀錄且其 `content_digest` 等於新內容的內容雜湊，否則 403
         `authorization_required`
    2. 現有內容不存在或沒有標記時，以新內容判斷：新內容標記 `requires_authorization`
       且 state 不是 active → 同樣要求有效核准紀錄。唯一例外是 state `archived` 且帶
       `note_id` 或 `legacy_archive`（`tasks migrate` 遷入的本機舊封存；doctor 會
       比對本機封存目錄）。`pending_apply` 一律要求紀錄（段二落地會讀它）
    3. 現有內容解析不了時視為沒有標記；新內容在規則 2 之外解析不了時照舊放行（通用側載）

    回傳值：守衛讀到的版本（不存在為 0），呼叫端以它做 CAS，讀與寫之間被改過就
    409 `version_conflict`；呼叫端自帶不同的 `expected_version` 時直接衝突。"""
    name = key[len(CHANGE_PREFIX) :]
    try:
        current = storage_sidecar.get(conn, vault, key, space=space)
    except NotFound:
        current = None
    version = current.version if current is not None else 0
    if expected_version is not None and expected_version != version:
        raise storage_sidecar.SidecarVersionConflict(expected_version, current, key)
    existing = _json_dict(current.content) if current is not None else None
    if existing is not None and tf.requires_authorization(existing):
        new = _change_doc(content, name)
        if not tf.requires_authorization(new):
            raise TaskChangeWriteForbidden(
                "authorization_downgrade_forbidden",
                f"change {name} 標記 requires_authorization: true，不可取消"
                "（需要時請使用者本人處理）",
            )
        needs = new.get("state") != STATE_ACTIVE or tf.bookkeeping(
            new
        ) != tf.bookkeeping(existing)
    else:
        new = _json_dict(content)
        needs = (
            new is not None
            and tf.requires_authorization(new)
            and new.get("state") != STATE_ACTIVE
        )
        if needs and new is not None:
            new = _change_doc(content, name)
            meta = new["meta"]
            if new["state"] == tf.STATE_ARCHIVED and (
                meta.get("note_id") or meta.get(tf.LEGACY_ARCHIVE_KEY)
            ):
                needs = False
    if needs:
        assert new is not None
        found = _record(conn, vault or "", name, space)
        if found is None or found[0].get("content_digest") != tf.authorization_digest(
            new
        ):
            raise TaskChangeWriteForbidden(
                "authorization_required",
                f"change {name} 標記 requires_authorization: true；"
                "封存或寫入 archive 簿記需要使用者在 UI 核准目前內容的紀錄",
            )
    return version


# ── 端點 ─────────────────────────────────────────────────────────────


def _read_change(conn: Any, vault: str, name: str) -> tuple[dict[str, Any], int]:
    blob = storage_sidecar.get(conn, vault, CHANGE_PREFIX + name, space=TASK_SPACE)
    return _change_doc(blob.content, name), blob.version


def _check_name(name: str) -> None:
    if not NAME_RE.match(name) or name == "archive":
        raise ValueError(
            f"change 名稱 {name!r} 只能用小寫英數與 -（kebab-case），且不可為 archive"
        )


@router.post("/tasks_authorize")
def tasks_authorize(request: Request, req: TasksAuthorizeRequest) -> dict[str, Any]:
    """核准一個 `requires_authorization` 的 change 的目前內容；回
    `{"record": 授權紀錄, "version": 紀錄的側載版本, "created": bool}`。"""
    state = _state(request)
    if req.space != TASK_SPACE:
        raise ValueError("任務層只屬於 dev space")
    name = req.change
    _check_name(name)
    if not state.runtime.current().tasks.remote_sync:
        raise TasksRemoteSyncDisabled(
            "服務未開啟任務層遠端同步（設定 tasks.remote_sync）；無法寫入授權紀錄"
        )
    principal = principal_of(request)
    display = display_of(request) or principal
    with state.connection() as conn:
        vault = resolve_write(conn, req.vault, space=TASK_SPACE)
        doc, version = _read_change(conn, vault, name)
        if doc.get("state") != STATE_ACTIVE:
            raise TaskAuthorizationRejected(
                "change_not_active",
                f"change {name} 的狀態是 {doc.get('state')!r}，只有進行中（active）"
                "的 change 可以核准",
            )
        if not tf.requires_authorization(doc):
            raise TaskAuthorizationRejected(
                "authorization_not_required",
                f"change {name} 沒有標記 requires_authorization，不需要核准",
            )
        digest = tf.authorization_digest(doc)
        found = _record(conn, vault, name)
        if found is not None and found[0].get("content_digest") == digest:
            record, record_version = found
            return {"record": record, "version": record_version, "created": False}
        record = {
            "schema": SCHEMA,
            "vault": vault,
            "change": name,
            "change_version": version,
            "content_digest": digest,
            "authorized_by": display,
            "authorized_at": _now(),
            "principal": {"kind": PRINCIPAL_UI, "name": principal},
        }
        written = storage_sidecar.put(
            conn,
            vault,
            AUTHORIZATION_PREFIX + name,
            tf.encode(record),
            space=TASK_SPACE,
            mime=MIME,
        )
    log.info(
        "任務核准：vault=%s change=%s change_version=%d content_digest=%s "
        "principal=%s display=%s",
        vault,
        name,
        version,
        digest,
        principal,
        display,
    )
    return {"record": record, "version": written.version, "created": True}


@router.post("/tasks_authorization_status")
def tasks_authorization_status(
    request: Request, req: TasksAuthorizeRequest
) -> dict[str, Any]:
    """唯讀：`{"change": null | {version, state, requires_authorization,
    content_digest}, "record": null | 授權紀錄, "approved": bool}`。

    change 不存在回 `change: null`；內容格式不合格 409 `change_invalid`；紀錄不存在或
    格式不合格（例如缺 `content_digest` 的舊紀錄）回 `record: null`。"""
    state = _state(request)
    if req.space != TASK_SPACE:
        raise ValueError("任務層只屬於 dev space")
    name = req.change
    _check_name(name)
    with state.connection() as conn:
        vault = resolve_write(conn, req.vault, space=TASK_SPACE)
        try:
            doc, version = _read_change(conn, vault, name)
        except NotFound:
            doc, version = None, 0
        found = _record(conn, vault, name)
    record = found[0] if found is not None else None
    if doc is None:
        return {"change": None, "record": record, "approved": False}
    digest = tf.authorization_digest(doc)
    return {
        "change": {
            "version": version,
            "state": doc["state"],
            "requires_authorization": tf.requires_authorization(doc),
            "content_digest": digest,
        },
        "record": record,
        "approved": record is not None and record.get("content_digest") == digest,
    }
