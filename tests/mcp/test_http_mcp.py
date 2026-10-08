"""D12：服務內建的 Streamable HTTP MCP 端點 `/mcp`。

- 走真正的 `create_app`，以 TestClient（會跑 lifespan）直接送 JSON-RPC；
  另以 MCP SDK client（legacy handshake 與 2026-07-28 兩種模式）驗證互通
- 認證與 `/v1/*` 相同；工具定義與 stdio 殼完全一致；HTTP 模式的差異
  （vault_resolve 用 remote_url、upload 收 base64、沒有 cwd／path）
- 不讀 os.environ、不開網路埠
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.binding import resolve_binding
from lore_vault.config import (
    Config,
    DocumentsConfig,
    EmbeddingConfig,
    McpConfig,
    Secret,
)
from lore_vault.mcp import task_plugin
from lore_vault.mcp.http import LoopbackTransport, build_http_server
from lore_vault.mcp.server import (
    HTTP_INSTRUCTIONS,
    INSTRUCTIONS,
    MODE_HTTP,
    Shell,
    build_server,
)
from lore_vault.mcp.settings import ShellSettings

from .conftest import DIM, TOKEN, NullEmbedder, add_vault

AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCEPT = {"Accept": "application/json, text/event-stream"}
PRINCIPAL = "tester"
MAX_FILE = 4096
HANDSHAKE_VERSION = "2025-06-18"


def _settings(db_path: Path, blob_dir: Path, **overrides) -> ApiSettings:
    base: dict[str, Any] = {
        "db_path": db_path,
        "snapshot_cache_dir": db_path.parent / "snapshot-cache",
        "token": Secret(TOKEN),
        "config": Config(
            embedding=EmbeddingConfig(dim=DIM),
            documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=MAX_FILE),
        ),
        "query_embedder": NullEmbedder(),
        "enrich_worker": False,
        "embedding_warmup": False,
        "document_worker": False,
        "principal": PRINCIPAL,
    }
    base.update(overrides)
    return ApiSettings(**base)


@pytest.fixture
def blob_dir(tmp_path) -> Path:
    path = tmp_path / "blobs"
    path.mkdir()
    return path


@pytest.fixture
def http(db_path, blob_dir):
    with TestClient(create_app(_settings(db_path, blob_dir))) as client:
        yield client


class McpSession:
    """以 JSON-RPC 直接打 `/mcp`（legacy handshake，帶 Mcp-Session-Id）。"""

    def __init__(self, client: TestClient, headers: dict[str, str] | None = None):
        self.client = client
        self.headers = {**ACCEPT, **(AUTH if headers is None else headers)}
        self.session_id: str | None = None
        self._id = 0

    def _post(self, payload: dict[str, Any]):
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
            headers["mcp-protocol-version"] = HANDSHAKE_VERSION
        return self.client.post("/mcp", json=payload, headers=headers)

    def request(self, method: str, params: dict[str, Any] | None = None):
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload)

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        assert resp.status_code == 200, resp.text
        self.session_id = resp.headers["mcp-session-id"]
        note = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.status_code == 202, note.text
        return resp.json()["result"]

    def tools(self) -> list[dict[str, Any]]:
        resp = self.request("tools/list")
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]["tools"]

    def call(self, name: str, **args) -> tuple[bool, dict[str, Any]]:
        resp = self.request("tools/call", {"name": name, "arguments": args})
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            text = text[text.index("{") :]
        return bool(result.get("isError")), json.loads(text)

    def ok(self, name: str, **args) -> dict[str, Any]:
        is_error, payload = self.call(name, **args)
        assert not is_error, payload
        return payload

    def err(self, name: str, **args) -> dict[str, Any]:
        is_error, payload = self.call(name, **args)
        assert is_error, payload
        return payload


def _open(client: TestClient) -> McpSession:
    session = McpSession(client)
    session.initialize()
    return session


# ── 認證 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-wrong-wrong-wrong"}, {"Authorization": "x"}],
    ids=["missing", "wrong", "malformed"],
)
def test_mcp_rejects_unauthenticated(http, headers):
    resp = McpSession(http, headers=headers).request(
        "initialize",
        {
            "protocolVersion": HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "unauthorized"
    assert "mcp-session-id" not in resp.headers


def test_mcp_session_cannot_be_reused_without_token(http):
    session = _open(http)
    anon = McpSession(http, headers={})
    anon.session_id = session.session_id
    assert anon.request("tools/list").status_code == 401


# ── 與 stdio 殼共用同一份工具定義 ────────────────────────────────────


def _stdio_shell() -> Shell:
    return Shell(
        ShellSettings(
            base_url="http://127.0.0.1:9", token=Secret(TOKEN), snapshot_on_start=False
        )
    )


@pytest.mark.anyio
async def test_tools_list_matches_stdio_exactly(http, db_path, blob_dir):
    over_http = _open(http).tools()
    shell = _stdio_shell()
    server = build_server(shell)
    task_plugin.register(server, shell)
    stdio_tools = await server.list_tools()
    stdio = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        }
        for t in stdio_tools
    ]
    got = [
        {k: t.get(k) for k in ("name", "description", "inputSchema")} for t in over_http
    ]
    assert got == stdio
    names = [t["name"] for t in got]
    # 核心 13 個＋任務層 tasks（經 task_plugin，兩種模式同一份定義）
    assert len(names) == 14 and names[-1] == "tasks"
    assert "upload" in names and "vault_resolve" in names
    assert {"download", "delete", "undelete"} <= set(names)
    # 兩步式與墓碑可還原要寫在工具說明裡（兩種模式同一份）
    by_name = {t["name"]: t for t in got}
    assert "confirm_token" in by_name["delete"]["description"]
    assert "不要自動連打兩步" in by_name["delete"]["description"]
    assert "undelete" in by_name["delete"]["description"]


def test_http_instructions_explain_remote_url(http):
    result = _open(http).initialize()
    assert result["instructions"] == HTTP_INSTRUCTIONS != INSTRUCTIONS
    assert "remote_url" in HTTP_INSTRUCTIONS
    assert "git remote get-url origin" in HTTP_INSTRUCTIONS
    assert "content_base64" in HTTP_INSTRUCTIONS


# ── vault_resolve：remote_url／key，cwd 在 HTTP 模式明確拒絕 ─────────


def _git_repo(path: Path, remote: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
    )
    return path


REMOTE_FORMS = [
    "git@github.com:Some-Owner/Some.Repo.git",
    "https://github.com/Some-Owner/Some.Repo",
    "https://user:token@github.com/Some-Owner/Some.Repo.git",
    "ssh://git@github.com:22/Some-Owner/Some.Repo",
]


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
@pytest.mark.parametrize("remote", REMOTE_FORMS)
def test_vault_resolve_remote_url_matches_cwd_binding(http, db_path, tmp_path, remote):
    """HTTP 的 remote_url 與 stdio 在該 repo 目錄以 cwd 解析得到同一個 key。"""
    binding = resolve_binding(_git_repo(tmp_path / "repo", remote))
    add_vault(db_path, binding.key, display="Some.Repo")
    session = _open(http)
    result = session.ok("vault_resolve", remote_url=remote)
    assert result["key"] == binding.key == "github.com/some-owner/some.repo"
    assert result["binding"] == {
        "key": binding.key,
        "display": binding.display,
        "source": "remote_url",
    }


def test_vault_resolve_remote_url_create_and_key(http):
    session = _open(http)
    missing = session.err("vault_resolve", remote_url="git@github.com:U/New.git")
    assert missing["error"]["code"] == "unknown_vault"
    created = session.ok(
        "vault_resolve", remote_url="git@github.com:U/New.git", create=True
    )
    assert (created["key"], created["display"], created["created"]) == (
        "github.com/u/new",
        "New",
        True,
    )
    by_key = session.ok("vault_resolve", key="github.com/u/new")
    assert by_key["key"] == "github.com/u/new" and by_key["created"] is False


def test_vault_resolve_cwd_is_rejected_over_http(http, tmp_path):
    session = _open(http)
    err = session.err("vault_resolve", cwd=str(tmp_path))
    assert err["error"]["code"] == "cwd_not_supported"
    assert "remote_url" in err["hint"]
    err = session.err("vault_resolve")
    assert err["error"]["code"] == "remote_url_required"
    # key／remote_url 已給時 cwd 只被忽略（標示），不當錯誤
    add_vault(http.app.state.lore.settings.db_path, "folder/x")
    ok = session.ok("vault_resolve", key="folder/x", cwd=str(tmp_path))
    assert ok["cwd_ignored"] is True


# ── upload：檔名 + base64，不收路徑 ─────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_upload_base64_creates_document(http, db_path, blob_dir):
    add_vault(db_path, "github.com/u/docs")
    session = _open(http)
    content = "# 標題\n\n內容段落。\n".encode()
    result = session.ok(
        "upload",
        vault="github.com/u/docs",
        filename="notes.md",
        content_base64=_b64(content),
    )
    assert result["status"] == "pending" and result["document_id"]
    assert result["vault_source"] == "explicit" and "path" not in result
    again = session.ok(
        "upload",
        vault="github.com/u/docs",
        filename="notes.md",
        content_base64=_b64(content),
    )
    assert again["duplicate"] is True
    listed = session.ok("list", vault="github.com/u/docs", kinds=["document"])
    assert [i["id"] for i in listed["items"]] == [result["document_id"]]


def test_upload_rejects_path_and_bad_content_over_http(http, db_path, tmp_path):
    add_vault(db_path, "github.com/u/docs")
    session = _open(http)
    local = tmp_path / "a.md"
    local.write_text("x", encoding="utf-8")
    err = session.err("upload", path=str(local), vault="github.com/u/docs")
    assert err["error"]["code"] == "path_not_supported"
    err = session.err("upload", filename="a.md", content_base64=_b64(b"x"))
    assert err["error"]["code"] == "vault_required"
    err = session.err(
        "upload", vault="github.com/u/docs", filename="a.md", content_base64="@@@"
    )
    assert err["error"]["code"] == "invalid_request"
    for bad in ("../a.md", "dir/a.md", "dir\\a.md", ".."):
        err = session.err(
            "upload",
            vault="github.com/u/docs",
            filename=bad,
            content_base64=_b64(b"x"),
        )
        assert err["error"]["code"] == "invalid_request", bad
    err = session.err(
        "upload",
        vault="github.com/u/docs",
        filename="big.md",
        content_base64=_b64(b"x" * (MAX_FILE + 1)),
    )
    assert err["error"]["code"] == "too_large"


# ── principal、session、fail closed ─────────────────────────────────


# ── download／delete／undelete ──────────────────────────────────────


def test_download_returns_base64_over_http(http, db_path):
    add_vault(db_path, "github.com/u/docs")
    session = _open(http)
    content = "# 下載\n\n原始內容。\n".encode()
    up = session.ok(
        "upload",
        vault="github.com/u/docs",
        filename="原檔.md",
        content_base64=_b64(content),
    )
    result = session.ok("download", vault="github.com/u/docs", id=up["document_id"])
    assert base64.b64decode(result["content_base64"]) == content
    assert result["filename"] == "原檔.md" and result["size_bytes"] == len(content)
    assert result["sha256"] == up["sha256"] and "path" not in result
    err = session.err(
        "download", vault="github.com/u/docs", id=up["document_id"], path="x.md"
    )
    assert err["error"]["code"] == "path_not_supported"
    assert "stdio" in err["hint"]


def test_download_over_http_enforces_size_limit(db_path, blob_dir):
    add_vault(db_path, "github.com/u/docs")
    config = Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=MAX_FILE),
        mcp=McpConfig(http_download_max_bytes=16),
    )
    with TestClient(create_app(_settings(db_path, blob_dir, config=config))) as c:
        session = _open(c)
        up = session.ok(
            "upload",
            vault="github.com/u/docs",
            filename="big.md",
            content_base64=_b64(b"x" * 17),
        )
        err = session.err("download", vault="github.com/u/docs", id=up["document_id"])
        assert err["error"]["code"] == "too_large"
        assert err["error"]["limit_bytes"] == 16
        assert "stdio" in err["hint"] and "UI" in err["hint"]
        small = session.ok(
            "upload",
            vault="github.com/u/docs",
            filename="small.md",
            content_base64=_b64(b"y" * 16),
        )
        ok = session.ok("download", vault="github.com/u/docs", id=small["document_id"])
        assert base64.b64decode(ok["content_base64"]) == b"y" * 16


def test_delete_two_step_over_http(http, db_path):
    add_vault(db_path, "github.com/u/docs")
    session = _open(http)
    note = session.ok("write", vault="github.com/u/docs", title="t", body="b")
    planned = session.ok("delete", vault="github.com/u/docs", id=note["id"])
    assert planned["executed"] is False and planned["next_step"]
    listed = session.ok("list", vault="github.com/u/docs")
    assert note["id"] in {i["id"] for i in listed["items"]}
    done = session.ok(
        "delete",
        vault="github.com/u/docs",
        id=note["id"],
        confirm_token=planned["confirm_token"],
    )
    assert done["executed"] is True
    listed = session.ok("list", vault="github.com/u/docs")
    assert note["id"] not in {i["id"] for i in listed["items"]}
    restored = session.ok("undelete", id=note["id"])
    assert restored["restored"] is True and restored["kind"] == "note"


def test_write_over_http_uses_credential_principal(http, db_path):
    add_vault(db_path, "github.com/u/w")
    session = _open(http)
    wrote = session.ok(
        "write", vault="github.com/u/w", title="t", body="b", author="Minka"
    )
    assert (wrote["author"], wrote["principal"]) == ("Minka", PRINCIPAL)


def test_space_is_per_mcp_session(http):
    a, b = _open(http), _open(http)
    assert a.ok("space", action="set", value="lore")["space"] == "lore"
    assert a.ok("space", action="get")["space"] == "lore"
    assert b.ok("space", action="get")["space"] == "dev"
    status = a.ok("status")
    assert status["mcp"] == {"mode": MODE_HTTP, "space": "lore"}
    assert "shell" not in status


@pytest.mark.anyio
async def test_loopback_without_caller_credentials_fails_closed(db_path, blob_dir):
    """拿不到呼叫端的認證 header 時不以服務 token 代打：內層請求回 401。"""
    app = create_app(_settings(db_path, blob_dir))
    server = build_http_server(app, app.state.lore.settings)
    async with Client(server) as client:  # in-memory：沒有 HTTP header 可轉發
        result = await client.call_tool("status", {})
    assert result.is_error
    payload = json.loads(result.content[0].text[result.content[0].text.index("{") :])
    assert payload["http_status"] == 401


@pytest.mark.anyio
async def test_space_set_without_session_is_refused(db_path, blob_dir):
    app = create_app(_settings(db_path, blob_dir))
    shell = Shell(
        ShellSettings(base_url="http://x", token=Secret(TOKEN)),
        transport=LoopbackTransport(app=app),
        mode=MODE_HTTP,
    )
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="session_required"):
        shell.space_tool("set", "lore")
    assert shell.space_tool("get")["space"] == "dev"
    await shell.aclose()


# ── MCP SDK client 互通 ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_sdk_client_over_http(db_path, blob_dir, mode):
    """真正的 SDK client（legacy handshake 與預設 auto → 2026-07-28）能列工具、呼叫。"""
    app = create_app(_settings(db_path, blob_dir))
    add_vault(db_path, "github.com/u/sdk")
    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://lore.test",
            headers=AUTH,
        )
        async with http_client:
            transport = streamable_http_client(
                "http://lore.test/mcp", http_client=http_client
            )
            async with Client(transport, mode=mode) as client:
                tools = await client.list_tools()
                assert len(tools.tools) == 14
                result = await client.call_tool(
                    "vault_resolve", {"remote_url": "https://github.com/U/SDK.git"}
                )
                assert not result.is_error, result.content[0].text
                assert json.loads(result.content[0].text)["key"] == "github.com/u/sdk"


# ── stdio 殼也接受 remote_url 與內容上傳（同一份定義；cwd／path 流程不變）──


@pytest.mark.anyio
async def test_stdio_shell_accepts_remote_url_and_content(db_path, blob_dir):
    from .conftest import Session, asgi, make_shell, session

    app = create_app(_settings(db_path, blob_dir))
    add_vault(db_path, "github.com/u/both")
    shell = make_shell(asgi(app), max_upload_bytes=MAX_FILE)
    async with session(shell) as client:
        s = Session(client)
        resolved = await s.ok("vault_resolve", remote_url="git@github.com:U/Both.git")
        assert resolved["key"] == "github.com/u/both"
        uploaded = await s.ok(
            "upload",
            vault="github.com/u/both",
            filename="a.md",
            content_base64=_b64(b"# a\n"),
        )
        assert uploaded["status"] == "pending" and "path" not in uploaded
        both = await s.err(
            "upload", path="a.md", filename="a.md", content_base64=_b64(b"x")
        )
        assert both["error"]["code"] == "invalid_request"
