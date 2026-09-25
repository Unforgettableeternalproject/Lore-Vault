"""MCP 殼測試共用：真正的 `create_app` 經 httpx2 ASGI transport 接到殼，不開網路埠。

- ASGITransport 不跑 lifespan：資料庫先以 `connect()` 遷移好，補算 worker 關閉
- Cloudflare Access 以 `CfEdge` 模擬：header 不符回 403（HTML），符合才轉給服務
- 不讀 os.environ，設定一律以參數注入
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client.client import Client

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, EmbeddingConfig, Secret
from lore_vault.mcp.server import Shell, build_server
from lore_vault.mcp.settings import ShellSettings
from lore_vault.schema import Vault
from lore_vault.storage.db import connect
from lore_vault.storage.vaults import upsert_vault

DIM = 16
TOKEN = "mcp-test-token-0123456789"
CF_ID = "cf-client-id-abcdef.access"
CF_SECRET = "cf-client-secret-0123456789abcdef"
BASE_URL = "http://lore.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class NullEmbedder:
    def embed(self, text: str) -> list[float]:
        raise ConnectionError("測試不使用 embedding")


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "lore.db"
    connect(path).close()  # ASGITransport 不跑 lifespan，先遷移
    return path


@pytest.fixture
def app(db_path):
    return create_app(
        ApiSettings(
            db_path=db_path,
            snapshot_cache_dir=db_path.parent / "snapshot-cache",
            token=Secret(TOKEN),
            config=Config(embedding=EmbeddingConfig(dim=DIM)),
            query_embedder=NullEmbedder(),
            enrich_worker=False,
            embedding_warmup=False,
        )
    )


@pytest.fixture
def snapshot_dir(tmp_path) -> Path:
    return tmp_path / "snapshot"


def add_vault(db_path: Path, key: str, display: str | None = None, aliases=()) -> None:
    conn = connect(db_path)
    try:
        upsert_vault(
            conn, Vault(key=key, display=display or key, aliases=tuple(aliases))
        )
    finally:
        conn.close()


class CfEdge:
    """模擬 Cloudflare Access 邊緣：service token 不符回 403；記錄收到的 header。"""

    def __init__(self, app, client_id: str = CF_ID, secret: str = CF_SECRET) -> None:
        self.app = app
        self.expected = (client_id.encode(), secret.encode())
        self.seen: list[dict[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        self.seen.append(headers)
        got = (
            headers.get("cf-access-client-id", "").encode(),
            headers.get("cf-access-client-secret", "").encode(),
        )
        if got != self.expected:
            body = b"<html>Forbidden</html>"
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/html")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class HeaderRecorder:
    """記錄每個請求的 header 後原樣轉給服務（本機直連情境）。"""

    def __init__(self, app) -> None:
        self.app = app
        self.seen: list[dict[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.seen.append(
                {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            )
        await self.app(scope, receive, send)


def asgi(app) -> httpx2.ASGITransport:
    return httpx2.ASGITransport(app=app)


def asgi_no_raise(app) -> httpx2.ASGITransport:
    """app 內未處理的例外回 500，而不是在測試端直接拋出。"""
    return httpx2.ASGITransport(app=app, raise_app_exceptions=False)


def failing(exc_factory) -> httpx2.MockTransport:
    """每個請求都拋指定的傳輸層例外（模擬連線失敗、逾時）。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_factory(request)

    return httpx2.MockTransport(handler)


def status_transport(status: int, body: Any = None, headers=None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if isinstance(body, dict):
            return httpx2.Response(status, json=body, headers=headers)
        return httpx2.Response(status, text=body or "", headers=headers)

    return httpx2.MockTransport(handler)


def make_shell(transport, snapshot_dir: Path | None = None, **overrides) -> Shell:
    settings = {
        "base_url": BASE_URL,
        "token": Secret(TOKEN),
        "snapshot_dir": snapshot_dir,
        "snapshot_on_start": False,
        "timeout": 5.0,
    }
    settings.update(overrides)
    return Shell(ShellSettings(**settings), transport=transport)


class Session:
    """in-process MCP client：`await s.call(name, **args)` → (is_error, payload)。"""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def call(self, name: str, **args) -> tuple[bool, dict[str, Any]]:
        result = await self.client.call_tool(name, args)
        text = result.content[0].text
        if result.is_error:
            # SDK 會在錯誤前加 "Error executing tool <name>: "，其後是殼給的 JSON
            text = text[text.index("{") :]
        return bool(result.is_error), json.loads(text)

    async def ok(self, name: str, **args) -> dict[str, Any]:
        is_error, payload = await self.call(name, **args)
        assert not is_error, payload
        return payload

    async def err(self, name: str, **args) -> dict[str, Any]:
        is_error, payload = await self.call(name, **args)
        assert is_error, payload
        return payload


def session(shell: Shell) -> Client:
    return Client(build_server(shell))
