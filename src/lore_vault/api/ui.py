"""使用者 UI（A21／A23）：`/ui/api/*` 登入端點、`/ui` 靜態檔（SPA fallback）與安全標頭。

- `GET /ui/api/login`：登入頁用的公開狀態（是否已設定帳號、是否鎖定、剩餘次數）；
  不含帳號名稱與任何機密
- `POST /ui/api/login`：`{"username": "...", "password": "..."}`（DB 內的 UI 帳號，
  A23）→ 204 + HttpOnly／SameSite=Strict／Path=/（可設定 Secure）的 session cookie。
  全域失敗 3 次即鎖定（423），需人工 `cli.admin ui-unlock --yes`
- `POST /ui/api/logout`：註銷目前 session 並清 cookie（沒有 session 也回 204）
- `GET /ui/api/session`：目前 session 的 principal、顯示名稱、期限與前端需要的限制值
  （`limits`）；無效回 401
- 以上都要求 `X-Lore-Vault-UI: 1`（CSRF；登入也要，擋跨站登入），不列入 OpenAPI
- 靜態檔本身不需登入（登入頁要能載入），資料一律走需認證的 API
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TypeGuard
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lore_vault.config import ConfigError
from lore_vault.notes.service import (
    DEFAULT_GET_BUDGET,
    DEFAULT_LIST_BUDGET,
    DEFAULT_LIST_LIMIT,
    MAX_GET_IDS,
    MAX_LIST_LIMIT,
)
from lore_vault.recall.service import DEFAULT_BUDGET as RECALL_DEFAULT_BUDGET
from lore_vault.recall.service import DEFAULT_LIMIT as RECALL_DEFAULT_LIMIT
from lore_vault.recall.service import MAX_LIMIT as RECALL_MAX_LIMIT
from lore_vault.schema import AUTHOR_MAX_CHARS
from lore_vault.storage import ui_login

from .ui_auth import UiAuth, client_ip, has_ui_header, read_cookie

log = logging.getLogger("lore_vault.api.ui")

# 登入 body 上限：帳號密碼本身很短，擋掉拿大 body 耗記憶體
MAX_LOGIN_BODY = 4096
# 登入頁與錯誤訊息提示的設定指令（密碼只在主機互動輸入，不經過 UI 或 agent）
SETUP_COMMAND = (
    "docker exec -it lore-vault python -m lore_vault.cli.admin ui-set-password "
    '--user <名稱> --display "<顯示名稱>"'
)

# 嚴格 CSP：無 inline script／style、無第三方來源（字型自託管）
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)


def _error(
    status: int, code: str, message: str, extra: dict[str, object] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, **(extra or {})}},
        status_code=status,
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _ui(request: Request) -> UiAuth:
    return request.app.state.ui_auth


def limits(request: Request) -> dict[str, int]:
    """前端需要的限制值（與服務端實際檢查同一來源；文件上限取服務的設定）。"""
    docs = request.app.state.lore.settings.config.documents
    return {
        "max_file_bytes": docs.max_file_bytes,
        "max_chars": docs.max_chars,
        "author_max_chars": AUTHOR_MAX_CHARS,
        "get_max_ids": MAX_GET_IDS,
        "get_default_budget": DEFAULT_GET_BUDGET,
        "list_max_limit": MAX_LIST_LIMIT,
        "list_default_limit": DEFAULT_LIST_LIMIT,
        "list_default_budget": DEFAULT_LIST_BUDGET,
        "recall_max_limit": RECALL_MAX_LIMIT,
        "recall_default_limit": RECALL_DEFAULT_LIMIT,
        "recall_default_budget": RECALL_DEFAULT_BUDGET,
    }


def _now(ui: UiAuth) -> datetime:
    return datetime.fromtimestamp(ui.clock(), UTC)


def _attempt(
    request: Request, username: str, password: str, ip: str
) -> ui_login.LoginOutcome:
    """在 threadpool 執行（scrypt 是 CPU 工作）；同一時間只跑一個嘗試。"""
    ui = _ui(request)
    with ui.login_lock, request.app.state.lore.connection() as conn:
        return ui_login.attempt_login(
            conn,
            username,
            password,
            ip=ip,
            now=_now(ui),
            retention_days=ui.login_log_retention_days,
        )


def _login_failure(outcome: ui_login.LoginOutcome) -> JSONResponse:
    status = outcome.status
    extra: dict[str, object] = {
        "remaining": status.remaining,
        "max_failures": ui_login.MAX_FAILURES,
        "locked": status.locked,
    }
    if outcome.result == "no_account":
        return _error(
            409,
            "no_account",
            f"尚未設定 UI 帳號，請在主機執行：{SETUP_COMMAND}",
            {**extra, "setup_command": SETUP_COMMAND},
        )
    if status.locked:
        prefix = "帳號或密碼錯誤；" if outcome.result == "bad_credentials" else ""
        return _error(
            423,
            "locked",
            f"{prefix}登入已鎖定（失敗達 {ui_login.MAX_FAILURES} 次），需人工解鎖",
            extra,
        )
    return _error(
        401,
        "invalid_credentials",
        f"帳號或密碼錯誤，剩餘 {status.remaining} 次；"
        f"失敗 {ui_login.MAX_FAILURES} 次將鎖定，需人工解鎖",
        extra,
    )


def _valid_login_body(data: object) -> TypeGuard[dict[str, str]]:
    return (
        isinstance(data, dict)
        and set(data) == {"username", "password"}
        and isinstance(data["username"], str)
        and isinstance(data["password"], str)
        and bool(data["username"].strip())
        and bool(data["password"])
    )


def build_router() -> APIRouter:
    """UI 登入用 DB 內的帳號密碼（A23）；Bearer 路徑的憑證表不經過這裡。"""
    router = APIRouter(prefix="/ui/api", include_in_schema=False)

    @router.get("/login")
    def login_state(request: Request) -> Response:
        ui = _ui(request)
        if not has_ui_header(request.scope):
            return _error(403, "csrf_required", "請求必須帶 X-Lore-Vault-UI: 1")
        with request.app.state.lore.connection() as conn:
            configured = ui_login.has_accounts(conn)
            status = ui_login.lock_status(conn, _now(ui))
        return JSONResponse(
            {
                "account_configured": configured,
                "locked": status.locked,
                "remaining": status.remaining,
                "max_failures": ui_login.MAX_FAILURES,
                "setup_command": None if configured else SETUP_COMMAND,
            }
        )

    @router.post("/login")
    async def login(request: Request) -> Response:
        ui = _ui(request)
        ip = client_ip(request.scope, ui.trusted_proxies)
        if not has_ui_header(request.scope):
            log.warning("UI 登入被拒（缺 CSRF 標頭） ip=%s", ip)
            return _error(403, "csrf_required", "登入請求必須帶 X-Lore-Vault-UI: 1")
        # 邊讀邊檢查長度：未認證端點不能讓人塞大 body 耗記憶體
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_LOGIN_BODY:
                log.warning("UI 登入被拒（body 過大） ip=%s", ip)
                return _error(413, "too_large", "登入請求 body 過大")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        if not _valid_login_body(data):
            # 格式錯不算猜測失敗，但同樣記 log；不回傳任何請求內容
            log.warning("UI 登入被拒（body 格式錯） ip=%s", ip)
            return _error(
                400,
                "invalid_request",
                'body 必須是 {"username": "<帳號>", "password": "<密碼>"}',
            )
        outcome = await run_in_threadpool(
            _attempt, request, data["username"], data["password"], ip
        )
        if not outcome.ok:
            log.warning(
                "UI 登入失敗 ip=%s result=%s remaining=%d",
                ip,
                outcome.result,
                outcome.status.remaining,
            )
            return _login_failure(outcome)
        account = outcome.account
        assert account is not None
        session_id = ui.sessions.create(account.username, account.display)
        log.info("UI 登入成功 ip=%s user=%s", ip, account.username)
        response = Response(status_code=204)
        response.set_cookie(
            ui.cookie_name,
            session_id,
            max_age=int(ui.sessions.absolute_seconds),
            path="/",
            secure=ui.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/logout")
    def logout(request: Request) -> Response:
        ui = _ui(request)
        if not has_ui_header(request.scope):
            return _error(403, "csrf_required", "請求必須帶 X-Lore-Vault-UI: 1")
        session_id = read_cookie(request.scope, ui.cookie_name)
        if session_id is not None and ui.sessions.revoke(session_id):
            log.info("UI 登出 ip=%s", client_ip(request.scope, ui.trusted_proxies))
        response = Response(status_code=204)
        response.delete_cookie(
            ui.cookie_name,
            path="/",
            secure=ui.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/session")
    def session(request: Request) -> Response:
        ui = _ui(request)
        if not has_ui_header(request.scope):
            return _error(403, "csrf_required", "請求必須帶 X-Lore-Vault-UI: 1")
        session_id = read_cookie(request.scope, ui.cookie_name)
        info = ui.sessions.touch(session_id) if session_id is not None else None
        if info is None:
            return _error(401, "unauthorized", "未登入或 session 已過期")
        return JSONResponse(
            {
                "authenticated": True,
                "principal": info.principal,
                "display_name": info.display_name,
                "expires_at": _iso(info.absolute_expires),
                "idle_expires_at": _iso(info.idle_expires),
                "limits": limits(request),
            }
        )

    @router.api_route(
        "/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
    )
    def not_found(rest: str) -> Response:
        # /ui/api/* 未知路徑回 JSON 404，不落到 SPA fallback
        return _error(404, "not_found", "不存在的 UI API 端點")

    return router


# 建置產物與常見靜態資源的副檔名：缺檔時照常 404，不回 index.html（否則缺的 .js 會變成
# 「Unexpected token <」這類難追的錯誤）。其餘路徑一律視為前端路由——vault key 常含「.」
# （github.com/org/u.e.p-s-core），不能用「最後一段有沒有點」判斷。
STATIC_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".map",
        ".css",
        ".json",
        ".webmanifest",
        ".txt",
        ".xml",
        ".html",
        ".htm",
        ".wasm",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".webm",
        ".ogg",
        ".wav",
        ".pdf",
    }
)
# 這個目錄底下只有建置產物，缺檔一律 404
ASSET_DIR = "assets"


def is_spa_route(path: str) -> bool:
    """`/ui` 底下（掛載點之後）的路徑是否該 fallback 到 index.html。

    path 先解碼：`%2F` 編碼的 vault key 要拆成段落再看最後一段。
    """
    parts = [p for p in unquote(path).replace("\\", "/").split("/") if p]
    if not parts:
        return True
    if parts[0] == ASSET_DIR:
        return False
    return PurePosixPath(parts[-1]).suffix.lower() not in STATIC_SUFFIXES


class SpaStaticFiles(StaticFiles):
    """找不到檔案時回 index.html（前端路由）。

    靜態資源（assets/ 與常見資源副檔名）缺檔照常 404。
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not is_spa_route(path):
                raise
            return await super().get_response("index.html", scope)


def static_app(static_dir: str) -> SpaStaticFiles:
    """設定了 static_dir 卻找不到 index.html 時拒絕啟動（不默默少了 UI）。"""
    root = Path(static_dir)
    if not (root / "index.html").is_file():
        raise ConfigError(f"ui.static_dir 找不到 index.html：{root}")
    return SpaStaticFiles(directory=root, html=True)


class UiSecurityHeadersMiddleware:
    """對 `/ui`、`/ui/*` 的回應加安全標頭；API 回應另加 no-store。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/ui" or path.startswith("/ui/")):
            await self.app(scope, receive, send)
            return
        extra = list(SECURITY_HEADERS)
        if path.startswith("/ui/api/"):
            extra.append((b"cache-control", b"no-store"))
        elif not path.startswith("/ui/assets/"):
            # index.html 與 fallback：每次重新驗證，才拿得到新版的雜湊資源檔名
            extra.append((b"cache-control", b"no-cache"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in extra}
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in names
                ]
                message = {**message, "headers": headers + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)
