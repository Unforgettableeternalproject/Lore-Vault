"""MCP `ask` 工具（D11）：薄殼轉發 `/v1/ask`、注入目前 space、用 ask_timeout、
服務錯誤原樣轉成工具錯誤、服務不可達不降級。"""

from __future__ import annotations

import json

import httpx2
import pytest

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.ask.client import Completion
from lore_vault.config import Config, EmbeddingConfig, Secret
from lore_vault.mcp.server import INSTRUCTIONS, build_server

from .conftest import (
    DIM,
    TOKEN,
    NullEmbedder,
    Session,
    add_vault,
    asgi,
    failing,
    make_shell,
    session,
)

pytestmark = pytest.mark.anyio
VAULT = "folder/ask-mcp"


class FakeAnswerer:
    model = "fake"

    def __init__(self, cite: list[str] | None = None) -> None:
        self.cite = cite
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> Completion:
        self.prompts.append(user)
        ids = self.cite or []
        payload = {
            "status": "answered" if ids else "insufficient",
            "points": [{"claim": "結論", "note_ids": ids}] if ids else [],
        }
        return Completion(json.dumps(payload), "fake-model", {"total_tokens": 3})


def _app(db_path, answerer):
    return create_app(
        ApiSettings(
            db_path=db_path,
            snapshot_cache_dir=db_path.parent / "snapshot-cache",
            token=Secret(TOKEN),
            config=Config(embedding=EmbeddingConfig(dim=DIM)),
            query_embedder=NullEmbedder(),
            enrich_worker=False,
            embedding_warmup=False,
            answerer=answerer,
        )
    )


class Recording(httpx2.AsyncBaseTransport):
    """記錄請求 body 與逾時後轉給 ASGI app。"""

    def __init__(self, app) -> None:
        self.inner = asgi(app)
        self.requests: list[tuple[str, dict, dict]] = []

    async def handle_async_request(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body, request.extensions["timeout"]))
        return await self.inner.handle_async_request(request)


async def test_ask_forwards_and_returns_service_json(db_path):
    add_vault(db_path, VAULT)
    fake = FakeAnswerer()
    transport = Recording(_app(db_path, fake))
    shell = make_shell(transport, ask_timeout=77.0)
    async with session(shell) as client:
        s = Session(client)
        wrote = await s.ok(
            "write", vault=VAULT, title="部署方式", body="服務以 docker 常駐"
        )
        fake.cite = [wrote["id"]]
        data = await s.ok(
            "ask", question="docker 部署", vault=VAULT, k=3, kinds=["note"]
        )
    assert data["status"] == "answered"
    assert data["answer"]["points"][0]["note_ids"] == [wrote["id"]]
    assert [src["id"] for src in data["sources"]] == [wrote["id"]]
    assert data["k"] == 3
    # 殼的 embedder 會失敗 → 檢索降級（只走 lexical），旗標原樣帶回
    assert data["degraded"] is True
    path, body, timeout = transport.requests[-1]
    assert path == "/v1/ask"
    assert body == {
        "question": "docker 部署",
        "vault": VAULT,
        "k": 3,
        "kinds": ["note"],
        "space": "dev",
    }
    assert timeout["read"] == 77.0
    # 其他工具仍用一般逾時
    assert transport.requests[0][2]["read"] == 5.0


async def test_ask_uses_current_space(db_path):
    add_vault(db_path, VAULT)
    transport = Recording(_app(db_path, FakeAnswerer()))
    async with session(make_shell(transport)) as client:
        s = Session(client)
        await s.ok("space", action="set", value="lore")
        err = await s.err("ask", question="q", vault=VAULT)
    assert transport.requests[-1][1]["space"] == "lore"
    assert err["error"]["code"] == "unknown_vault"


async def test_ask_service_errors_become_tool_errors_with_hint(db_path):
    add_vault(db_path, VAULT)
    shell = make_shell(asgi(_app(db_path, None)))  # 沒有 key → ask_not_configured
    async with session(shell) as client:
        s = Session(client)
        await s.ok("write", vault=VAULT, title="部署方式", body="docker 常駐")
        err = await s.err("ask", question="docker 部署", vault=VAULT)
        assert err["error"]["code"] == "ask_not_configured"
        assert err["http_status"] == 500
        assert "recall" in err["hint"]
        err = await s.err("ask", question="docker", vault=VAULT, kinds=["chunk"])
        assert err["error"]["code"] == "unsupported_kind"


async def test_ask_unreachable_is_error_without_degrade(tmp_path):
    shell = make_shell(
        failing(lambda req: httpx2.ConnectError("refused", request=req)),
        snapshot_dir=tmp_path / "snapshot",
    )
    async with session(shell) as client:
        err = await Session(client).err("ask", question="q", vault=VAULT)
    assert err["error"]["code"] == "service_unreachable"
    assert "無法降級" in err["error"]["message"]
    assert "degraded" not in err["error"]


async def test_ask_tool_description_warns_about_confidence(db_path):
    server = build_server(make_shell(asgi(_app(db_path, None))))
    tools = {t.name: t for t in await server.list_tools()}
    description = tools["ask"].description
    assert "信心有限" in description and "get" in description
    assert "唯一事實" in description and "唯一事實" in INSTRUCTIONS
    assert "ask" in INSTRUCTIONS
