"""服務層例外 → HTTP 狀態碼與統一錯誤格式 `{"error": {"code", "message", ...}}`。

Starlette 依例外類別的 MRO 找 handler：子類別（VaultRequired 是 ValueError）
會先配到自己的 handler，最後才落到 ValueError → 400。
TypeError 不接：型別已由 pydantic 擋下，漏網的是程式錯誤，應該是 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lore_vault.ask import AskError, AskRateLimited
from lore_vault.documents.service import UploadRejected
from lore_vault.notes import InvalidCursor, NoChanges, VersionConflict
from lore_vault.recall import UnsupportedKind
from lore_vault.schema import InvalidCharacters
from lore_vault.storage.admin import (
    NeedsForce,
    NotRestorable,
    PlanChanged,
    SpaceChangeRefused,
)
from lore_vault.storage.errors import (
    DuplicateRecord,
    InvalidSpace,
    NotFound,
    ReservedVault,
    SpaceKeyPrefixRequired,
    SpaceRequired,
    StorageError,
    UnknownVault,
    VaultConflict,
    VaultRequired,
)
from lore_vault.storage.manage import AliasConflict, CannotRemoveKey, RetryRefused
from lore_vault.storage.settings_store import InvalidSettings
from lore_vault.storage.sidecar import (
    InvalidSidecarKey,
    SidecarTooLarge,
    SidecarVersionConflict,
)

CREATE_VAULT_HINT = (
    "以 POST /v1/vaults 建立（key、display；key 由客戶端用 lore_vault.binding 算出），"
    "或確認 key 是否打錯；write 不會自動建立 vault"
)


class TasksRemoteSyncDisabled(Exception):
    """服務關閉任務層遠端同步（`tasks.remote_sync = false`，D15 MCP 已裁決）。

    403 + `tasks_remote_sync_disabled`：`task-` 開頭的側載 `blob_put` 一律拒收、
    不寫任何東西；讀取（`blob_get`）與其他 key（如 `tasks-snapshot`）不受影響。"""


class PayloadTooLarge(Exception):
    """上傳超過大小上限（讀取 body 時就擋，不讀完整份）。"""


class DocumentsNotConfigured(Exception):
    """服務未設定 documents.blob_dir，文件功能關閉。"""


class VaultExists(Exception):
    """建 vault 時 key 或別名已存在（建 vault 端點只新增、不覆寫）。"""

    def __init__(self, message: str, existing: dict[str, Any]) -> None:
        super().__init__(message)
        self.existing = existing


class EpisodeIngestDisabled(Exception):
    """服務關閉 episode 收料（`episodes.ingest = false`，D13）。

    403 + `episode_ingest_disabled`：客戶端 spool 依錯誤碼辨識，檔案留在本機、
    拉長退避，不當成暫時錯誤頻繁重試，也不移到 rejected。"""


class UiSessionRequired(Exception):
    """只允許以 UI 登入（session cookie）操作的管理端點，收到 bearer 請求。"""


class ConfirmTokenInvalid(ValueError):
    """確認 token 無法解析、簽章不符，或與這次請求的操作／參數不符（竄改或誤用）。"""


class ConfirmTokenExpired(ValueError):
    """確認 token 已過期：重新規劃取得新 token。"""


class ConfirmPlanChanged(Exception):
    """規劃後資料已變動：token 綁定的規劃內容與執行當下重新規劃的結果不同。

    附目前的規劃與綁定它的新 token（需使用者再確認一次才以新 token 重送）。
    """

    def __init__(
        self,
        message: str,
        plan: dict[str, Any],
        *,
        confirm_token: str,
        expires_at: str,
    ) -> None:
        super().__init__(message)
        self.plan = plan
        self.confirm_token = confirm_token
        self.expires_at = expires_at


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _json(status: int, code: str, exc: BaseException, **extra: Any) -> JSONResponse:
    return JSONResponse(error_body(code, str(exc), **extra), status_code=status)


def _conflict_note(exc: VersionConflict) -> dict[str, Any]:
    """衝突時回目前版本的中繼資料（不含 body：全文走 get，維持 A4 的預算）。"""
    note = exc.current
    return {
        "id": note.id,
        "vault": note.vault,
        "title": note.title,
        "topics": list(note.topics),
        "links": list(note.links),
        "supersedes": note.supersedes,
        "author": note.author,
        "updated_by": note.updated_by,
        "created": note.created,
        "updated": note.updated,
    }


def install_error_handlers(app: FastAPI) -> None:
    def simple(exc_type: type[Exception], status: int, code: str) -> None:
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return _json(status, code, exc)

        app.add_exception_handler(exc_type, handler)

    simple(VaultRequired, 400, "vault_required")
    simple(SpaceRequired, 400, "space_required")
    simple(InvalidSpace, 400, "invalid_space")
    simple(SpaceKeyPrefixRequired, 400, "space_key_prefix_required")
    simple(ReservedVault, 400, "reserved_vault")
    simple(NotFound, 404, "not_found")
    simple(NoChanges, 400, "no_changes")
    simple(InvalidCursor, 400, "invalid_cursor")
    simple(UnsupportedKind, 400, "unsupported_kind")
    simple(DuplicateRecord, 409, "duplicate")
    simple(VaultConflict, 409, "vault_conflict")
    # 服務層的參數驗證（query 為空、limit 超出範圍、schema 驗證失敗等）
    simple(ValueError, 400, "invalid_request")
    # 其餘儲存層錯誤（schema 版本不符、庫內向量維度不符等）是伺服器端資料問題，
    # 不可因為它們同時是 ValueError 而被當成 400
    simple(StorageError, 500, "storage_error")

    async def unknown_vault(request: Request, exc: Exception) -> JSONResponse:
        return _json(404, "unknown_vault", exc, hint=CREATE_VAULT_HINT)

    async def version_conflict(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, VersionConflict)
        return _json(
            409,
            "version_conflict",
            exc,
            expected=exc.expected,
            current=_conflict_note(exc),
        )

    async def vault_exists(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, VaultExists)
        return _json(409, "vault_exists", exc, existing=exc.existing)

    async def invalid_characters(request: Request, exc: Exception) -> JSONResponse:
        # 只回欄位、字元索引與碼位，不回顯內容
        assert isinstance(exc, InvalidCharacters)
        return _json(
            400,
            "invalid_characters",
            exc,
            field=exc.field,
            index=exc.index,
            codepoint=f"U+{exc.codepoint:04X}",
            kind=exc.kind,
        )

    async def upload_rejected(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, UploadRejected)
        return _json(413 if exc.code == "too_large" else 400, exc.code, exc)

    # 管理端點（api.manage）
    simple(SpaceChangeRefused, 400, "space_change_refused")
    simple(CannotRemoveKey, 400, "cannot_remove_key")
    simple(PlanChanged, 409, "plan_changed")
    simple(NeedsForce, 409, "needs_force")
    simple(ConfirmTokenInvalid, 400, "invalid_confirm_token")
    simple(ConfirmTokenExpired, 400, "confirm_token_expired")

    async def confirm_plan_changed(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ConfirmPlanChanged)
        return _json(
            409,
            "plan_changed",
            exc,
            plan=exc.plan,
            confirm_token=exc.confirm_token,
            expires_at=exc.expires_at,
        )

    async def alias_conflict(request: Request, exc: Exception) -> JSONResponse:
        # 佔用者在別的 space 時 existing 為 null（不透露存在性以外的資訊）
        assert isinstance(exc, AliasConflict)
        existing = {"key": exc.existing} if exc.existing is not None else None
        return _json(409, "vault_exists", exc, existing=existing)

    async def with_reason(request: Request, exc: Exception) -> JSONResponse:
        # NotRestorable → 409 not_restorable（附 reason）；RetryRefused → 409 reason
        if isinstance(exc, NotRestorable):
            return _json(409, "not_restorable", exc, reason=exc.reason)
        assert isinstance(exc, RetryRefused)
        return _json(409, exc.reason, exc)

    async def ask_error(request: Request, exc: Exception) -> JSONResponse:
        # 刻意不用 502／503／504：MCP 殼把那些視為「服務不可達」（mcp.client），
        # 會誤報成服務掛了。模型失敗一律 500（429 除外），以 code 區分
        assert isinstance(exc, AskError)
        extra: dict[str, Any] = {}
        if isinstance(exc, AskRateLimited):
            extra["retry_after"] = exc.retry_after
        return _json(exc.http_status, exc.code, exc, **extra)

    app.add_exception_handler(AskError, ask_error)
    app.add_exception_handler(ConfirmPlanChanged, confirm_plan_changed)
    app.add_exception_handler(AliasConflict, alias_conflict)
    app.add_exception_handler(NotRestorable, with_reason)
    app.add_exception_handler(RetryRefused, with_reason)

    async def invalid_settings(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, InvalidSettings)
        return _json(
            400,
            "invalid_setting",
            exc,
            errors=[
                {"key": e.key, "code": e.code, "message": str(e)} for e in exc.errors
            ],
        )

    # 執行期設定（D13）
    app.add_exception_handler(InvalidSettings, invalid_settings)
    simple(EpisodeIngestDisabled, 403, "episode_ingest_disabled")
    simple(TasksRemoteSyncDisabled, 403, "tasks_remote_sync_disabled")
    simple(UiSessionRequired, 403, "ui_session_required")

    simple(PayloadTooLarge, 413, "too_large")
    # 側載（schema v17）：兩者都是 ValueError，各自的 handler 先於 invalid_request
    simple(InvalidSidecarKey, 400, "invalid_key")
    simple(SidecarTooLarge, 413, "too_large")
    simple(DocumentsNotConfigured, 500, "documents_not_configured")
    app.add_exception_handler(UploadRejected, upload_rejected)
    app.add_exception_handler(InvalidCharacters, invalid_characters)
    app.add_exception_handler(UnknownVault, unknown_vault)
    app.add_exception_handler(VersionConflict, version_conflict)

    async def sidecar_version_conflict(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # 形狀比照 note update：`expected`＋`current`；側載的 current 附完整內容
        # （設計 TASK_LAYER_MCP §1.2：呼叫端據此 rebase 後帶新版本重送）
        assert isinstance(exc, SidecarVersionConflict)
        current = None if exc.current is None else exc.current.to_dict()
        return _json(
            409, "version_conflict", exc, expected=exc.expected, current=current
        )

    app.add_exception_handler(SidecarVersionConflict, sidecar_version_conflict)
    app.add_exception_handler(VaultExists, vault_exists)
