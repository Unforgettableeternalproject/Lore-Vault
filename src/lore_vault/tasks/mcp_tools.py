"""任務層的 MCP 工具定義（TASK_LAYER_MCP §3）；由 `lore_vault.mcp.task_plugin` 掛載。

核心不 import 本模組；只有組合層 `mcp/task_plugin.py`（isolation 的唯一具名例外）
在 `mcp.tasks_enabled` 為真時動態載入，呼叫 `build_tools(shell)` 取得工具清單，
再以與核心工具相同的慣例（`structured_output=False`）註冊到 stdio 與 HTTP 兩邊的
server。

新增工具（MCP-T3 起）只需在 `build_tools` 回傳的清單加一筆 `TaskTool`：
- `name` 不可與核心工具（`lore_vault.mcp.server.TOOL_NAMES`）重名，plugin 會拒絕
- `fn` 是 async 函式，參數以 `Annotated[..., Field(description=...)]` 描述，最後一個
  參數為 `ctx: Context | None = None`，本體包在 `with shell.request_scope(ctx):`
  （HTTP 模式依 session 取目前 space 與轉發認證 header），回傳緊湊 JSON 字串
- 對服務的請求一律走 `shell` 既有的 async `ServiceClient`
  （經 `shell._send` 注入 space），不要另起同步 client
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from lore_vault.mcp.server import Shell


class TaskTool(NamedTuple):
    name: str
    fn: Callable[..., Any]
    description: str


def build_tools(shell: Shell) -> list[TaskTool]:
    """任務層要掛上 MCP server 的工具；`tasks(action=)` 由 MCP-T3 加入。"""
    return []
