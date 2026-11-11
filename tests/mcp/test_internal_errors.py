"""非預期例外的共用包裝（`guard_tool`）與殼的輪替 log 檔。

FastMCP 對未處理的例外只回「Error executing tool <name>」；包裝把它轉成帶例外類別的
結構化 `internal_error`。核心工具與任務層工具都要經過同一個包裝（任務層的參數化測試在
`test_tasks_archive_mcp.py`）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from lore_vault.mcp import __main__ as shell_main
from lore_vault.mcp.server import Shell, _tool_error, guard_tool
from lore_vault.mcp.settings import default_log_path, load_shell_settings

from .conftest import TOKEN, Session, session
from .test_tasks_mcp import make_shell

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("missing"),
        RuntimeError(""),
        ImportError("cannot import name 'x'"),
        MemoryError(),
        OSError("disk gone"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_core_tool_unexpected_exception_is_structured(
    app, tmp_path, monkeypatch, caplog, exc
):
    async def explode(self, *args, **kwargs):
        raise exc

    monkeypatch.setattr(Shell, "recall", explode)
    async with session(make_shell(app, tmp_path)) as client:
        with caplog.at_level("ERROR", logger="lore_vault.mcp"):
            payload = await Session(client).err("recall", query="x", vault="*")
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"].startswith(type(exc).__name__)
    assert "重啟 MCP 殼" in payload["hint"]
    assert any(r.exc_info and r.exc_info[1] is exc for r in caplog.records)


async def test_guard_passes_tool_error_through_unchanged():
    original = _tool_error("not_found", "沒有這個")

    @guard_tool
    async def tool():
        raise original

    with pytest.raises(ToolError) as caught:
        await tool()
    assert caught.value is original


async def test_guard_does_not_swallow_cancellation():
    @guard_tool
    async def tool():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tool()


async def test_guard_keeps_signature_for_fastmcp():
    import inspect

    async def tool(a: int, b: str = "x") -> str:
        return b

    wrapped = guard_tool(tool)
    assert inspect.signature(wrapped) == inspect.signature(tool)
    assert wrapped.__name__ == "tool"


# ── 殼 log 檔 ───────────────────────────────────────────────────────


@pytest.fixture
def clean_root_logger():
    """還原 root logger 的 handler，避免測試之間汙染。"""
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()


def test_attach_file_log_writes_traceback_and_rotates(tmp_path, clean_root_logger):
    path = tmp_path / "logs" / "mcp-shell.log"
    assert shell_main.attach_file_log(path) is True
    handler = next(
        h
        for h in clean_root_logger.handlers
        if getattr(h, "lore_vault_mcp_shell_file", False)
    )
    assert handler.maxBytes == 1024 * 1024 and handler.backupCount == 3
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("lore_vault.mcp").exception("工具失敗")
    handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "工具失敗" in text and "RuntimeError: boom" in text and "Traceback" in text


def test_attach_file_log_is_idempotent(tmp_path, clean_root_logger):
    shell_main.attach_file_log(tmp_path / "a.log")
    shell_main.attach_file_log(tmp_path / "b.log")
    tagged = [
        h
        for h in clean_root_logger.handlers
        if getattr(h, "lore_vault_mcp_shell_file", False)
    ]
    assert len(tagged) == 1


def test_attach_file_log_failure_does_not_raise(tmp_path, clean_root_logger):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    # 父路徑是檔案：mkdir 失敗
    assert shell_main.attach_file_log(blocker / "sub" / "mcp.log") is False


def test_log_path_default_and_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert default_log_path() == home / ".lore-vault" / "logs" / "mcp-shell.log"
    env = {"LORE_VAULT_API_TOKEN": TOKEN}
    assert load_shell_settings(environ=env).log_path == default_log_path()
    custom = tmp_path / "custom.log"
    env["LORE_VAULT_MCP_LOG_PATH"] = str(custom)
    assert load_shell_settings(environ=env).log_path == Path(custom)


def test_shell_import_does_not_pull_api_layer():
    """殼（含任務層）啟動就載好 archive 要用的一切，且不拖進 fastapi／API 層；
    版本錯位因此在啟動時失敗，而不是拖到 archive 當下。"""
    import subprocess
    import sys

    code = (
        "import sys, lore_vault.tasks.mcp_tools;"
        "bad = [m for m in ('fastapi', 'lore_vault.api.manage',"
        " 'lore_vault.api.errors') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], check=False)
    assert done.returncode == 0
