"""服務內建的 Streamable HTTP MCP 端點 `/mcp`（D12）。

- 工具定義與 stdio 殼完全相同（`server.build_server`），`Shell` 以 `mode="http"` 建立
- 轉發方式：**in-process ASGI**（`LoopbackTransport`）打同一個 app 的 `/v1/*`，
  不經網路、不需要知道自己的埠。沿用呼叫端這次請求的認證 header
  （Authorization，或 UI cookie + X-Lore-Vault-UI），內層請求再走一次
  `BearerAuthMiddleware`，principal 判定與直接打 `/v1/*` 完全相同；
  拿不到呼叫端 header 時拿掉認證（fail closed，內層回 401），不以服務 token 代打
- 認證：`/mcp` 不是公開路徑，外層的 `BearerAuthMiddleware` 先擋（與 `/v1/*` 同一個
  token、同一套 principal）；MCP SDK 的 OAuth 不啟用
- 回應用 JSON（`json_response=True`）：工具不送進度通知，JSON 對代理最友善
- 有 session（`Mcp-Session-Id`）：目前 space 依 session 保存（`Shell.request_scope`）
- DNS rebinding 保護關閉：SDK 預設只收 localhost Host，經 tunnel／反向代理會被擋；
  這裡的保護是 bearer token（瀏覽器不會自動帶）
- 請求大小上限依 `documents.max_file_bytes` 的 base64 長度加餘裕（`upload` 走內容）
- 殼、MCP server 與 session manager 每次 lifespan 重建（SDK 的 `run()` 每個實例只能
  進一次；server 的 lifespan 結束時會關閉殼的 HTTP client）
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from .server import FORWARD_HEADERS, MODE_HTTP, Shell, build_server, forwarded_headers
from .settings import ShellSettings

if TYPE_CHECKING:
    from lore_vault.api.settings import ApiSettings
    from lore_vault.config import Config

MCP_PATH = "/mcp"
# in-process 轉發用的虛擬位址（不會真的連線）
LOOPBACK_BASE_URL = "http://lore-vault.loopback"
# JSON-RPC 包裝與其他參數的餘裕
_BODY_OVERHEAD = 1024 * 1024


def max_request_body(max_file_bytes: int) -> int:
    """`/mcp` 請求 body 上限：最大檔案 base64 後的長度 + 1MB 餘裕。"""
    return (max_file_bytes + 2) // 3 * 4 + _BODY_OVERHEAD


class LoopbackTransport(httpx2.ASGITransport):
    """把殼的 `/v1/*` 請求直接交給同一個 app；認證 header 換成呼叫端自己的。"""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        for name in FORWARD_HEADERS:
            if name in request.headers:
                del request.headers[name]
        forward = forwarded_headers()
        if forward:
            for name, value in forward.items():
                request.headers[name] = value
        return await super().handle_async_request(request)


class McpEndpoint:
    """掛在 `/mcp` 的 ASGI 端點：轉給目前 lifespan 內的 session manager。"""

    def __init__(self, factory: Callable[[], MCPServer], *, max_body: int) -> None:
        self.factory = factory
        self.max_body = max_body
        self._manager = None

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        server = self.factory()
        server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=True,
            max_request_body_size=self.max_body,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        manager = server.session_manager
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        manager = self._manager
        if manager is None:
            body = json.dumps(
                {"error": {"code": "mcp_unavailable", "message": "MCP 端點尚未啟動"}},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await manager.handle_request(scope, receive, send)


def _runtime_download_limit(app: object, config: Config) -> int:
    state = getattr(getattr(app, "state", None), "lore", None)
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return config.mcp.http_download_max_bytes
    return runtime.current().mcp.http_download_max_bytes


def build_http_server(app: object, settings: ApiSettings) -> MCPServer:
    """HTTP 模式的殼與 MCP server；`app` 是要轉發到的同一個 ASGI app。"""
    config = settings.config
    shell_settings = ShellSettings(
        base_url=LOOPBACK_BASE_URL,
        # 只作為 ServiceClient 的預設 header；每個請求都被 LoopbackTransport 換掉
        token=settings.token,
        timeout=config.mcp.timeout,
        snapshot_on_start=False,
        max_upload_bytes=config.documents.max_file_bytes,
        ask_timeout=config.mcp.ask_timeout,
        # download 以 base64 回傳（進 agent 上下文），上限另設、刻意較小
        download_max_bytes=config.mcp.http_download_max_bytes,
    )
    shell = Shell(
        shell_settings,
        # 內層 app 的未處理例外回 500 → ServiceError，不讓工具任務直接崩潰
        transport=LoopbackTransport(app=app, raise_app_exceptions=False),  # type: ignore[arg-type]
        mode=MODE_HTTP,
        # 上限可由設定頁調整（D13）：每次 download 讀服務的執行期有效值
        download_limit=lambda: _runtime_download_limit(app, config),
    )
    return build_server(shell)


def build_http_endpoint(app: object, settings: ApiSettings) -> McpEndpoint:
    return McpEndpoint(
        lambda: build_http_server(app, settings),
        max_body=max_request_body(settings.config.documents.max_file_bytes),
    )
