"""任務層 MCP 組合層（`mcp/task_plugin.py`，TASK_LAYER_MCP §2、MCP-T2）。

- 開關 `mcp.tasks_enabled` 關閉：不載入任務層、核心 13 個工具的清單與 schema 不變
- 開啟：`tasks.mcp_tools.build_tools` 回傳的工具以核心慣例掛上；不得與核心工具重名
- stdio（`__main__`）與 HTTP（`build_http_server`）兩個組裝入口都經過 plugin
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, EmbeddingConfig, McpConfig, Secret
from lore_vault.mcp import __main__ as mcp_main
from lore_vault.mcp import server as mcp_server
from lore_vault.mcp import settings as mcp_settings
from lore_vault.mcp import task_plugin
from lore_vault.mcp.http import build_http_server
from lore_vault.mcp.server import TOOL_NAMES, Shell, build_server
from lore_vault.mcp.settings import ShellSettings, load_shell_settings
from lore_vault.tasks import mcp_tools
from lore_vault.tasks.mcp_tools import TaskTool

from .conftest import DIM, TOKEN, NullEmbedder

pytestmark = pytest.mark.anyio


def _shell(*, enabled: bool) -> Shell:
    return Shell(
        ShellSettings(
            base_url="http://127.0.0.1:9",
            token=Secret(TOKEN),
            snapshot_on_start=False,
            tasks_enabled=enabled,
        )
    )


async def _tools(server: MCPServer) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        }
        for t in await server.list_tools()
    ]


async def _dummy(ctx: Any = None) -> str:
    return "{}"


def _fake_tools(*names: str):
    def build(shell: Shell) -> list[TaskTool]:
        return [TaskTool(n, _dummy, f"假工具 {n}") for n in names]

    return build


def _boom(shell: Shell) -> list[Any]:
    raise AssertionError("開關關閉時不得載入任務層")


async def test_disabled_does_not_touch_tasks_and_keeps_core_tools(monkeypatch):
    monkeypatch.setattr(task_plugin, "_load_tools", _boom)
    shell = _shell(enabled=False)
    server = build_server(shell)
    before = await _tools(server)
    assert task_plugin.register(server, shell) == []
    after = await _tools(server)
    assert after == before
    assert [t["name"] for t in after] == list(TOOL_NAMES) and len(after) == 13


async def test_disabled_ignores_tasks_tools_even_if_defined(monkeypatch):
    monkeypatch.setattr(mcp_tools, "build_tools", _fake_tools("tasks"))
    shell = _shell(enabled=False)
    server = build_server(shell)
    task_plugin.register(server, shell)
    assert [t["name"] for t in await _tools(server)] == list(TOOL_NAMES)


async def test_enabled_mounts_real_tasks_tool_without_touching_core():
    """真正的 `build_tools`：只多一個 `tasks`，核心 13 個工具的 schema 不變。"""
    shell = _shell(enabled=True)
    server = build_server(shell)
    assert task_plugin.register(server, shell) == ["tasks"]
    tools = await _tools(server)
    assert [t["name"] for t in tools] == [*TOOL_NAMES, "tasks"]
    assert tools[:-1] == await _tools(build_server(_shell(enabled=True)))


async def test_enabled_mounts_task_tools(monkeypatch):
    monkeypatch.setattr(mcp_tools, "build_tools", _fake_tools("tasks"))
    shell = _shell(enabled=True)
    server = build_server(shell)
    assert task_plugin.register(server, shell) == ["tasks"]
    tools = await _tools(server)
    assert [t["name"] for t in tools] == [*TOOL_NAMES, "tasks"]
    assert tools[-1]["description"] == "假工具 tasks"
    # 核心工具的 schema 不受附加工具影響
    assert tools[:-1] == await _tools(build_server(_shell(enabled=False)))


@pytest.mark.parametrize("names", [("status",), ("space",), ("tasks", "tasks")])
async def test_task_tool_cannot_shadow_existing_tool(monkeypatch, names):
    monkeypatch.setattr(mcp_tools, "build_tools", _fake_tools(*names))
    shell = _shell(enabled=True)
    server = build_server(shell)
    core = await _tools(server)
    with pytest.raises(ValueError, match="重名"):
        task_plugin.register(server, shell)
    if names[0] in TOOL_NAMES:
        assert (await _tools(server))[: len(core)] == core


def test_load_shell_settings_reads_switch():
    base = {"LORE_VAULT_API_TOKEN": TOKEN}
    assert load_shell_settings(environ=base).tasks_enabled is True
    off = load_shell_settings(environ={**base, "LORE_VAULT_MCP_TASKS_ENABLED": "false"})
    assert off.tasks_enabled is False


# ── 組裝入口 ────────────────────────────────────────────────────────


def _api_settings(db_path: Path, *, enabled: bool) -> ApiSettings:
    return ApiSettings(
        db_path=db_path,
        snapshot_cache_dir=db_path.parent / "snapshot-cache",
        token=Secret(TOKEN),
        config=Config(
            embedding=EmbeddingConfig(dim=DIM),
            mcp=McpConfig(tasks_enabled=enabled),
        ),
        query_embedder=NullEmbedder(),
        enrich_worker=False,
        embedding_warmup=False,
        document_worker=False,
        principal="tester",
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_http_entry_goes_through_plugin(monkeypatch, db_path, enabled):
    monkeypatch.setattr(mcp_tools, "build_tools", _fake_tools("tasks"))
    app = create_app(_api_settings(db_path, enabled=enabled))
    server = build_http_server(app, app.state.lore.settings)
    names = [t["name"] for t in await _tools(server)]
    assert names == [*TOOL_NAMES, *(["tasks"] if enabled else [])]


@pytest.mark.parametrize("enabled", [True, False])
async def test_stdio_entry_goes_through_plugin(monkeypatch, enabled):
    monkeypatch.setattr(mcp_tools, "build_tools", _fake_tools("tasks"))
    settings = ShellSettings(
        base_url="http://127.0.0.1:9",
        token=Secret(TOKEN),
        snapshot_on_start=False,
        tasks_enabled=enabled,
    )
    monkeypatch.setattr(mcp_settings, "load_shell_settings", lambda **_: settings)
    built: list[MCPServer] = []
    real_build = mcp_server.build_server

    def build(shell: Shell) -> MCPServer:
        server = real_build(shell)
        monkeypatch.setattr(server, "run", lambda transport: built.append(server))
        return server

    monkeypatch.setattr(mcp_server, "build_server", build)
    assert mcp_main.main([]) == 0
    assert len(built) == 1
    names = [t["name"] for t in await _tools(built[0])]
    assert names == [*TOOL_NAMES, *(["tasks"] if enabled else [])]
