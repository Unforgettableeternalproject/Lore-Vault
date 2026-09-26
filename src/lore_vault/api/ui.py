"""使用者 UI（A21）：`/ui/api/*` 登入端點、`/ui` 靜態檔（SPA fallback）與安全標頭。

- `POST /ui/api/login`：`{"key": "<存取金鑰>"}`（= `LORE_VAULT_API_TOKEN`）→ 204 +
  HttpOnly／SameSite=Strict／Path=/（可設定 Secure）的 session cookie
- `POST /ui/api/logout`：註銷目前 session 並清 cookie（沒有 session 也回 204）
- `GET /ui/api/session`：目前 session 的期限；無效回 401
- 三者都要求 `X-Lore-Vault-UI: 1`（CSRF；登入也要，擋跨站登入），不列入 OpenAPI
- 靜態檔本身不需登入（登入頁要能載入），資料一律走需認證的 API
"""

from __future__ import annotations

import hmac
import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lore_vault.config import ConfigError, Secret

from .ui_auth import UiAuth, client_ip, has_ui_header, read_cookie

log = logging.getLogger("lore_vault.api.ui")

# 登入 body 上限：金鑰本身很短，擋掉拿大 body 耗記憶體
MAX_LOGIN_BODY = 4096

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


def _error(status: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers=headers or None,
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _ui(request: Request) -> UiAuth:
    return request.app.state.ui_auth


def build_router(token: Secret) -> APIRouter:
    router = APIRouter(prefix="/ui/api", include_in_schema=False)
    expected = token.reveal().encode("utf-8")

    @router.post("/login")
    async def login(request: Request) -> Response:
        ui = _ui(request)
        ip = client_ip(request.scope, ui.trusted_proxies)
        if not has_ui_header(request.scope):
            log.warning("UI 登入被拒（缺 CSRF 標頭） ip=%s", ip)
            return _error(403, "csrf_required", "登入請求必須帶 X-Lore-Vault-UI: 1")
        wait = ui.limiter.retry_after(ip)
        if wait > 0:
            log.warning("UI 登入被限流 ip=%s retry_after=%.0fs", ip, wait)
            return _error(
                429,
                "too_many_attempts",
                "登入失敗次數過多，請稍後再試",
                **{"Retry-After": str(max(1, math.ceil(wait)))},
            )
        # 邊讀邊檢查長度：未認證端點不能讓人塞大 body 耗記憶體
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_LOGIN_BODY:
                log.warning("UI 登入被拒（body 過大） ip=%s", ip)
                return _error(413, "too_large", "登入請求 body 過大")
        provided: object = None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and set(data) == {"key"}:
            provided = data["key"]
        if not isinstance(provided, str):
            # 格式錯不算猜測失敗，但同樣記 log；不回傳任何請求內容
            log.warning("UI 登入被拒（body 格式錯） ip=%s", ip)
            return _error(400, "invalid_request", 'body 必須是 {"key": "<存取金鑰>"}')
        if not hmac.compare_digest(provided.encode("utf-8"), expected):
            ui.limiter.record_failure(ip)
            log.warning("UI 登入失敗 ip=%s", ip)
            return _error(401, "invalid_credentials", "存取金鑰不正確")
        ui.limiter.record_success(ip)
        session_id = ui.sessions.create()
        log.info("UI 登入成功 ip=%s", ip)
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
                "expires_at": _iso(info.absolute_expires),
                "idle_expires_at": _iso(info.idle_expires),
            }
        )

    @router.api_route(
        "/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
    )
    def not_found(rest: str) -> Response:
        # /ui/api/* 未知路徑回 JSON 404，不落到 SPA fallback
        return _error(404, "not_found", "不存在的 UI API 端點")

    return router


class SpaStaticFiles(StaticFiles):
    """找不到檔案時回 index.html（前端路由）；帶副檔名的路徑照常 404。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            last = path.rsplit("/", 1)[-1]
            if exc.status_code != 404 or "." in last:
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
