"""API 層共用的認證守衛。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from .errors import UiSessionRequired
from .principals import AUTH_UI, auth_method_of

UI_ONLY_DEFAULT = "審核只能由登入 UI 的使用者本人操作"


def ui_session_only(message: str = UI_ONLY_DEFAULT) -> Callable[[Request], None]:
    """產生 router 依賴：bearer 一律 403 `ui_session_required`。

    在 body 驗證之前檢查（FastAPI 的 router 依賴先於請求 body 解析），bearer 拿不到
    422 等任何細節。"""

    def require(request: Request) -> None:
        if auth_method_of(request) != AUTH_UI:
            raise UiSessionRequired(message)

    return require


def ui_only_route(message: str = UI_ONLY_DEFAULT) -> type[APIRoute]:
    """UI session 限定的路由類別：認證檢查在**解析 body 之前**，bearer 送壞 JSON 也
    拿到 403 `ui_session_required`（router 依賴擋不住 body 解析錯誤的 422）。"""

    class UiOnlyRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
            original = super().get_route_handler()

            async def handler(request: Request) -> Response:
                if auth_method_of(request) != AUTH_UI:
                    raise UiSessionRequired(message)
                return await original(request)

            return handler

    return UiOnlyRoute
