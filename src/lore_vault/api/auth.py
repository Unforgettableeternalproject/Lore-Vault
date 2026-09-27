"""Bearer token 認證（A15）＋ UI session cookie（A21）：純 ASGI 中介層，預設拒絕。

- 公開路徑只有 `PUBLIC_PATHS`（`/healthz`）與 `/ui`、`/ui/*`（靜態檔與登入端點；
  `/ui/api/*` 自己處理認證）。其餘所有路徑都要認證，本機請求也不例外
- 認證方式二擇一：
  - `Authorization: Bearer <token>`：只要帶了 Authorization 標頭就只走這條，
    不再看 cookie（Bearer 路徑行為與 A15 完全相同）
  - 有效的 UI session cookie，且必須帶 `X-Lore-Vault-UI: 1`（CSRF 防護）；
    cookie 有效但缺標頭回 403 `csrf_required`
- 在路由與 body 解析之前檢查：未認證的請求拿不到 422／404 等任何內部資訊
- 常數時間比較（`hmac.compare_digest`）；缺少、格式錯、不符一律同一個 401，
  回應與 log 都不含請求帶來的值或正確 token
- Cloudflare Access 由邊緣處理，這裡不驗 Access JWT
- 認證通過時把憑證對應的 principal（A22，`api.principals`）放進 scope；Bearer 依
  token 查表，cookie 依 session 登入時記下的 principal
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lore_vault.config import Secret

from .principals import AUTH_UI, Principals, set_principal
from .ui_auth import UiAuth, has_ui_header, read_cookie

PUBLIC_PATHS = frozenset({"/healthz"})
UI_PREFIX = "/ui"

UNAUTHORIZED_BODY = {
    "error": {"code": "unauthorized", "message": "缺少或無效的 bearer token"}
}
CSRF_BODY = {
    "error": {
        "code": "csrf_required",
        "message": "以 session cookie 認證的請求必須帶 X-Lore-Vault-UI: 1",
    }
}


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def _has_authorization(scope: Scope) -> bool:
    return any(k.lower() == b"authorization" for k, _ in scope.get("headers", ()))


def _bearer(scope: Scope) -> bytes | None:
    """取出唯一一個 Authorization header 的 bearer 值；多個或格式不符回 None。"""
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, _, credential = values[0].partition(b" ")
    if scheme.lower() != b"bearer":
        return None
    credential = credential.strip()
    return credential or None


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: Secret,
        *,
        ui_auth: UiAuth | None = None,
        principals: Principals | None = None,
    ) -> None:
        self.app = app
        self._principals = principals or Principals.single(token)
        self._ui = ui_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # lifespan
            await self.app(scope, receive, send)
            return
        if (
            scope["type"] == "http"
            and scope["path"] == "/"
            and scope.get("method") in ("GET", "HEAD")
            and self._ui is not None
        ):
            # 經 pm 子網域進站的人預期看到 UI，而不是 401 JSON
            await send(
                {
                    "type": "http.response.start",
                    "status": 307,
                    "headers": [(b"location", b"/ui/"), (b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["type"] == "http" and is_public_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        if _has_authorization(scope) or self._ui is None:
            provided = _bearer(scope)
            principal = (
                self._principals.match(provided) if provided is not None else None
            )
            if principal is not None:
                set_principal(scope, principal)
                await self.app(scope, receive, send)
                return
        elif scope["type"] == "http":
            session_id = read_cookie(scope, self._ui.cookie_name)
            info = (
                self._ui.sessions.touch(session_id) if session_id is not None else None
            )
            if info is not None:
                if has_ui_header(scope):
                    set_principal(
                        scope,
                        info.principal,
                        method=AUTH_UI,
                        display=info.display_name,
                    )
                    await self.app(scope, receive, send)
                    return
                response = JSONResponse(CSRF_BODY, status_code=403)
                await response(scope, receive, send)
                return
        if scope["type"] == "websocket":
            # 沒有 websocket 端點；未認證的一律在握手前關閉
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            UNAUTHORIZED_BODY, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        await response(scope, receive, send)
