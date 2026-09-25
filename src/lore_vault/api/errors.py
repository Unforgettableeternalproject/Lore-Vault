"""服務層例外 → HTTP 狀態碼與統一錯誤格式 `{"error": {"code", "message", ...}}`。

Starlette 依例外類別的 MRO 找 handler：子類別（VaultRequired 是 ValueError）
會先配到自己的 handler，最後才落到 ValueError → 400。
TypeError 不接：型別已由 pydantic 擋下，漏網的是程式錯誤，應該是 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lore_vault.notes import InvalidCursor, NoChanges, VersionConflict
from lore_vault.recall import UnsupportedKind
from lore_vault.storage.errors import (
    DuplicateRecord,
    NotFound,
    StorageError,
    UnknownVault,
    VaultConflict,
    VaultRequired,
)

CREATE_VAULT_HINT = (
    "以 POST /v1/vaults 建立（key、display；key 由客戶端用 lore_vault.binding 算出），"
    "或確認 key 是否打錯；write 不會自動建立 vault"
)


class VaultExists(Exception):
    """建 vault 時 key 或別名已存在（建 vault 端點只新增、不覆寫）。"""

    def __init__(self, message: str, existing: dict[str, Any]) -> None:
        super().__init__(message)
        self.existing = existing


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
        "created": note.created,
        "updated": note.updated,
    }


def install_error_handlers(app: FastAPI) -> None:
    def simple(exc_type: type[Exception], status: int, code: str) -> None:
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return _json(status, code, exc)

        app.add_exception_handler(exc_type, handler)

    simple(VaultRequired, 400, "vault_required")
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

    app.add_exception_handler(UnknownVault, unknown_vault)
    app.add_exception_handler(VersionConflict, version_conflict)
    app.add_exception_handler(VaultExists, vault_exists)
