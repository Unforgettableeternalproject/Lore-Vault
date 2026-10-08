"""任務層 MCP 組合層：把 `lore_vault.tasks` 的工具掛上 stdio 與 HTTP 的 MCP server。

核心零 import 任務層（D15）唯一的具名例外（`tasks.isolation.CORE_EXEMPT_FILES`，
TASK_LAYER_MCP §2）。只被兩個組裝入口在 `build_server(shell)` 之後呼叫：
stdio 的 `mcp/__main__.py` 與 HTTP 的 `mcp/http.py::build_http_server`；
`mcp/server.py` 與其他核心模組不 import 本檔，也不經任何 `__init__.py` re-export。

- 開關 `mcp.tasks_enabled`（`LORE_VAULT_MCP_TASKS_ENABLED`，預設開啟）經
  `ShellSettings.tasks_enabled` 讀取；關閉時什麼都不做，連任務層模組都不載入，
  核心 13 個工具的清單與 schema 完全不變
- 任務層是主套件的一部分（沒有 optional extra），載入失敗是 bug，不吞例外
- 工具定義在 `lore_vault.tasks.mcp_tools.build_tools(shell)`；註冊慣例
  （`structured_output=False`）由這裡統一套用，任務層工具不得與核心工具重名
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .server import TOOL_NAMES

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .server import Shell


def _load_tools(shell: Shell) -> list[Any]:
    # 模組名刻意寫成字串常值：isolation 掃描以此認出這是任務層 import
    module = importlib.import_module("lore_vault.tasks.mcp_tools")
    return list(module.build_tools(shell))


def register(server: MCPServer, shell: Shell) -> list[str]:
    """掛上任務層工具，回傳實際註冊的工具名（關閉時為空）。"""
    if not shell.settings.tasks_enabled:
        return []
    registered: list[str] = []
    for tool in _load_tools(shell):
        if tool.name in TOOL_NAMES or tool.name in registered:
            raise ValueError(f"任務層工具 {tool.name!r} 與既有工具重名")
        server.add_tool(
            tool.fn,
            name=tool.name,
            description=tool.description,
            structured_output=False,
        )
        registered.append(tool.name)
    return registered
