"""以 MCP SDK client 啟動真正的 stdio 殼（`python -m lore_vault.mcp`）。"""

from __future__ import annotations

import sys

import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from lore_vault.mcp.server import TOOL_NAMES

from .conftest import TOKEN

pytestmark = pytest.mark.anyio

EXPECTED = {
    "space",
    "vault_resolve",
    "recall",
    "ask",
    "get",
    "list",
    "write",
    "update",
    "upload",
    "download",
    "delete",
    "undelete",
    "status",
}


async def test_stdio_lists_exactly_the_expected_tools(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lore_vault.mcp"],
        env={
            "LORE_VAULT_API_TOKEN": TOKEN,
            # 不可達的位址：殼仍須正常啟動（背景拉快照失敗只記 log）
            "LORE_VAULT_MCP_BASE_URL": "http://127.0.0.1:9",
            "LORE_VAULT_MCP_TIMEOUT": "1",
            "LORE_VAULT_MCP_SNAPSHOT_DIR": str(tmp_path / "snapshot"),
            "HOME": str(home),
            "USERPROFILE": str(home),
        },
        cwd=str(tmp_path),
    )
    with open(tmp_path / "stderr.log", "w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog)) as client:
            result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    # 核心工具＋任務層 `tasks`（`mcp.tasks_enabled` 預設開啟，經 task_plugin 掛上）
    assert names == EXPECTED | {"tasks"} and EXPECTED == set(TOOL_NAMES)
    for tool in result.tools:
        assert tool.description, tool.name
    # 刻意排除的能力沒有被暴露
    assert not names & {"chat", "create_vault", "settings", "source", "model"}
    log = (tmp_path / "stderr.log").read_text(encoding="utf-8")
    assert TOKEN not in log
