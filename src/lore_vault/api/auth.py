"""Bearer token 認證（A15）：純 ASGI 中介層，預設拒絕。

- 除了 `PUBLIC_PATHS`（`/healthz`）之外，所有路徑都要求
  `Authorization: Bearer <token>`，本機請求也不例外
- 在路由與 body 解析之前檢查：未認證的請求拿不到 422／404 等任何內部資訊
- 常數時間比較（`hmac.compare_digest`）；缺少、格式錯、不符一律同一個 401，
  回應與 log 都不含請求帶來的值或正確 token
- Cloudflare Access 由邊緣處理，這裡不看 CF header
"""

from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lore_vault.config import Secret

PUBLIC_PATHS = frozenset({"/healthz"})

UNAUTHORIZED_BODY = {
    "error": {"code": "unauthorized", "message": "缺少或無效的 bearer token"}
}


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
    def __init__(self, app: ASGIApp, token: Secret) -> None:
        self.app = app
        self._expected = token.reveal().encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # lifespan
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        provided = _bearer(scope)
        if provided is not None and hmac.compare_digest(provided, self._expected):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # 沒有 websocket 端點；未認證的一律在握手前關閉
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            UNAUTHORIZED_BODY, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        await response(scope, receive, send)
