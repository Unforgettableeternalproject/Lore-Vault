"""stdio 殼的專案目錄來源與守門：客戶端 root → LORE_VAULT_CWD → 行程 cwd，
以及系統目錄／磁碟根不當專案。

全部隔離全域狀態：環境變數用 monkeypatch、行程 cwd 用 monkeypatch.chdir、
WINDIR 指到 tmp 內的假系統目錄（不寫死機器路徑）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import types
from mcp.client.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.session import ServerSession

from lore_vault.mcp import server as server_module
from lore_vault.mcp.server import Shell, build_server
from lore_vault.mcp.workdir import CWD_ENV, is_system_dir, root_from_uri

from .conftest import Session, add_vault, asgi, make_shell
from .test_tasks_mcp import VAULT as TASKS_VAULT
from .test_tasks_mcp import Tasks, client_for, make_app
from .test_tasks_mcp import make_shell as make_tasks_shell

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_windir(tmp_path, monkeypatch) -> Path:
    """tmp 內的假 %WINDIR%；清掉 LORE_VAULT_CWD，避免外部環境污染結果。"""
    windir = tmp_path / "FakeWindows"
    system32 = windir / "system32"
    system32.mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(windir))
    monkeypatch.setenv("WINDIR", str(windir))
    monkeypatch.delenv(CWD_ENV, raising=False)
    return system32


@pytest.fixture
def project(tmp_path) -> Path:
    path = tmp_path / "Alpha"
    path.mkdir()
    return path


@pytest.fixture
def elsewhere(tmp_path, monkeypatch, fake_windir) -> Path:
    """行程 cwd 放在一個不是專案的普通目錄（不是系統目錄）。"""
    path = tmp_path / "elsewhere"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def roots_client(shell: Shell, callback, mode: str = "legacy") -> Client:
    # roots/list 是伺服器主動發的請求：只有 initialize 握手的協定有 back-channel，
    # 桌面客戶端目前走這條；2026-07-28 無狀態協定沒有（見下方 modern 測試）
    return Client(build_server(shell), list_roots_callback=callback, mode=mode)


def static_roots(*uris: str):
    calls: list[int] = []

    async def callback(context):
        calls.append(1)
        return types.ListRootsResult(roots=[types.Root(uri=u) for u in uris])

    callback.calls = calls  # type: ignore[attr-defined]
    return callback


async def resolved_cwd(client: Client) -> str:
    result = await Session(client).ok("vault_resolve")
    return result["binding"]["cwd"]


def same(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


# ── root_from_uri／is_system_dir ────────────────────────────────────


def test_root_from_uri_roundtrip_with_space_and_cjk(tmp_path):
    path = tmp_path / "我的 專案"
    assert root_from_uri(path.as_uri()) == path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 路徑形式")
def test_root_from_uri_windows_drive_and_percent_encoding():
    assert root_from_uri("file:///C:/Users/a%20b/proj") == Path("C:/Users/a b/proj")


@pytest.mark.parametrize("uri", ["https://example.com/x", "file://other-host/share/x"])
def test_root_from_uri_rejects_non_local(uri):
    assert root_from_uri(uri) is None


def test_is_system_dir_windir_and_root(fake_windir, tmp_path):
    assert is_system_dir(fake_windir)
    assert is_system_dir(fake_windir.parent)
    assert is_system_dir(Path(tmp_path.anchor))
    assert not is_system_dir(tmp_path)
    # 前綴相同但不是子目錄的兄弟目錄不算
    sibling = fake_windir.parent.with_name(fake_windir.parent.name + "X")
    assert not is_system_dir(sibling)


# ── 解析順序 ────────────────────────────────────────────────────────


async def test_client_root_preferred_over_process_cwd(app, db_path, project, elsewhere):
    add_vault(db_path, "folder/alpha", display="Alpha")
    callback = static_roots(project.as_uri())
    shell = make_shell(asgi(app))
    async with roots_client(shell, callback) as client:
        assert same(await resolved_cwd(client), project)
        assert same(await resolved_cwd(client), project)
    assert len(callback.calls) == 1  # 只問一次


async def test_client_root_wins_over_env(app, db_path, project, elsewhere, monkeypatch):
    add_vault(db_path, "folder/alpha", display="Alpha")
    other = elsewhere.parent / "envdir"
    other.mkdir()
    monkeypatch.setenv(CWD_ENV, str(other))
    shell = make_shell(asgi(app))
    async with roots_client(shell, static_roots(project.as_uri())) as client:
        assert same(await resolved_cwd(client), project)


async def test_modern_protocol_without_back_channel_falls_back(
    app, db_path, project, elsewhere, monkeypatch
):
    monkeypatch.chdir(project)
    add_vault(db_path, "folder/alpha", display="Alpha")
    callback = static_roots(elsewhere.as_uri())
    shell = make_shell(asgi(app))
    async with roots_client(shell, callback, mode="auto") as client:
        assert same(await resolved_cwd(client), project)
    assert callback.calls == []


async def test_no_roots_capability_never_calls_list_roots(
    app, db_path, project, elsewhere, monkeypatch
):
    calls: list[int] = []

    async def spy(self):
        calls.append(1)
        raise AssertionError("客戶端沒宣告 roots，不應呼叫 list_roots")

    monkeypatch.setattr(ServerSession, "list_roots", spy)
    monkeypatch.chdir(project)
    add_vault(db_path, "folder/alpha", display="Alpha")
    shell = make_shell(asgi(app))
    async with Client(build_server(shell)) as client:  # 沒有 list_roots_callback
        assert same(await resolved_cwd(client), project)
    assert calls == []


async def test_list_roots_error_falls_back_to_process_cwd(
    app, db_path, project, elsewhere, monkeypatch
):
    async def failing_callback(context):
        return types.ErrorData(code=types.INVALID_REQUEST, message="boom")

    monkeypatch.chdir(project)
    add_vault(db_path, "folder/alpha", display="Alpha")
    shell = make_shell(asgi(app))
    async with roots_client(shell, failing_callback) as client:
        assert same(await resolved_cwd(client), project)


async def test_list_roots_timeout_falls_back_without_hanging(
    app, db_path, project, elsewhere, monkeypatch
):
    async def slow_callback(context):
        await asyncio.sleep(30)
        return types.ListRootsResult(roots=[])

    monkeypatch.setattr(server_module, "ROOTS_TIMEOUT", 0.05)
    monkeypatch.chdir(project)
    add_vault(db_path, "folder/alpha", display="Alpha")
    shell = make_shell(asgi(app))
    async with roots_client(shell, slow_callback) as client:
        assert same(await asyncio.wait_for(resolved_cwd(client), 10), project)


async def test_no_usable_root_falls_back(app, db_path, project, elsewhere, monkeypatch):
    """沒有 root、或 root 指向不存在的目錄：都退回行程 cwd。"""
    monkeypatch.chdir(project)
    add_vault(db_path, "folder/alpha", display="Alpha")
    missing = (elsewhere / "不存在").as_uri()
    for roots in ((), (missing,)):
        shell = make_shell(asgi(app))
        async with roots_client(shell, static_roots(*roots)) as client:
            assert same(await resolved_cwd(client), project)


async def test_env_override_beats_process_cwd(
    app, db_path, project, elsewhere, monkeypatch
):
    add_vault(db_path, "folder/alpha", display="Alpha")
    monkeypatch.setenv(CWD_ENV, str(project))
    shell = make_shell(asgi(app))
    async with Client(build_server(shell)) as client:
        assert same(await resolved_cwd(client), project)


async def test_explicit_cwd_param_wins(app, db_path, project, elsewhere, tmp_path):
    add_vault(db_path, "folder/alpha", display="Alpha")
    beta = tmp_path / "Beta"
    beta.mkdir()
    add_vault(db_path, "folder/beta", display="Beta")
    shell = make_shell(asgi(app))
    async with roots_client(shell, static_roots(project.as_uri())) as client:
        result = await Session(client).ok("vault_resolve", cwd=str(beta))
        assert result["key"] == "folder/beta"


async def test_injected_cwd_is_authoritative(app, db_path, project, elsewhere):
    """建構時注入 cwd（測試用）優先於客戶端 root，既有測試行為不變。"""
    add_vault(db_path, "folder/alpha", display="Alpha")
    shell = make_shell(asgi(app))
    shell._cwd = lambda: str(project)
    other = elsewhere.parent / "rootdir"
    other.mkdir()
    async with roots_client(shell, static_roots(other.as_uri())) as client:
        assert same(await resolved_cwd(client), project)


async def test_status_reports_cwd_source(app, project, elsewhere):
    shell = make_shell(asgi(app))
    async with roots_client(shell, static_roots(project.as_uri())) as client:
        status = await Session(client).ok("status")
        cwd = status["shell"]["cwd"]
        assert cwd["source"] == "client_root" and cwd["is_project_dir"] is True
        assert same(cwd["path"], project)


# ── 守門 ────────────────────────────────────────────────────────────


async def test_vault_resolve_rejects_system_dir_process_cwd(
    app, db_path, fake_windir, monkeypatch
):
    monkeypatch.chdir(fake_windir)
    shell = make_shell(asgi(app))
    async with Client(build_server(shell)) as client:
        s = Session(client)
        err = await s.err("vault_resolve", create=True)
        assert err["error"]["code"] == "cwd_required"
        assert "cwd" in err["hint"]
        # 顯式 cwd 不套守門：使用者明示的目錄照常解析（這裡只是 vault 尚未建立）
        explicit = await s.err("vault_resolve", cwd=str(fake_windir))
        assert explicit["error"]["code"] == "unknown_vault"


async def test_vault_resolve_rejects_drive_root(app, tmp_path, fake_windir):
    shell = make_shell(asgi(app))
    shell._cwd = lambda: tmp_path.anchor
    async with Client(build_server(shell)) as client:
        err = await Session(client).err("vault_resolve")
        assert err["error"]["code"] == "cwd_required"


async def test_env_override_pointing_to_system_dir_is_rejected(
    app, fake_windir, elsewhere, monkeypatch
):
    monkeypatch.setenv(CWD_ENV, str(fake_windir))
    shell = make_shell(asgi(app))
    async with Client(build_server(shell)) as client:
        err = await Session(client).err("vault_resolve")
        assert err["error"]["code"] == "cwd_required"


def test_upload_roots_excludes_system_dir_but_keeps_configured(
    app, project, fake_windir, tmp_path
):
    extra = tmp_path / "extra"
    extra.mkdir()
    shell = make_shell(asgi(app), upload_roots=(extra,))
    shell._cwd = lambda: str(fake_windir)
    assert shell.upload_roots() == (extra,)
    shell._cwd = lambda: tmp_path.anchor
    assert shell.upload_roots() == (extra,)
    # 對照：專案目錄照常納入
    shell._cwd = lambda: str(project)
    assert shell.upload_roots() == (project, extra)


async def test_upload_without_vault_rejects_system_dir(app, fake_windir):
    shell = make_shell(asgi(app))
    shell._cwd = lambda: str(fake_windir)
    async with Client(build_server(shell)) as client:
        err = await Session(client).err(
            "upload", filename="a.md", content_base64="IyBoaQ=="
        )
        assert err["error"]["code"] == "cwd_required"


def test_download_without_path_rejects_system_dir(app, fake_windir):
    shell = make_shell(asgi(app))
    shell._cwd = lambda: str(fake_windir)
    with pytest.raises(ToolError) as info:
        shell._download_target(None, "a.md")
    assert "cwd_required" in str(info.value)


async def test_tasks_init_with_system_dir_creates_nothing(
    db_path, tmp_path, fake_windir, monkeypatch
):
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    add_vault(db_path, TASKS_VAULT)
    app = make_app(db_path)
    async with client_for(make_tasks_shell(app, fake_windir)) as client:
        t = Tasks(client)
        # 省略 vault：要靠工作目錄推算，直接拒絕
        err = await t.err(action="init")
        assert err["error"]["code"] == "cwd_required"
        # 明示 vault：遠端照常啟用，但不碰本機目錄
        result = await t.ok(action="init", vault=TASKS_VAULT)
        assert result["local"]["written"] is False
        assert "系統目錄" in result["local"]["reason"]
    assert not (fake_windir / "openspec").exists()
    assert list(fake_windir.iterdir()) == []


async def test_tasks_archive_gate_without_vault_rejects_system_dir(
    db_path, fake_windir, monkeypatch
):
    monkeypatch.delenv("LORE_VAULT_TASKS_ROOT", raising=False)
    add_vault(db_path, TASKS_VAULT)
    app = make_app(db_path)
    async with client_for(make_tasks_shell(app, fake_windir)) as client:
        err = await Tasks(client).err(action="archive", name="x")
        assert err["error"]["code"] == "cwd_required"
