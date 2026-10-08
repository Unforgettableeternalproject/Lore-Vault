"""MCP `tasks` 的啟用狀態（`list` 的 enabled／local_initialized）與停用（`disable`）。

- `list` 回 `enabled`（有索引且未停用）、`disabled`；stdio 另回 `local_initialized`
  （本機 openspec/ 且 remote: true），服務端已啟用而本機未初始化時附 `next_step`
- `disable` 冪等；停用中除 list／init／disable 外的 action 一律 `tasks_disabled`，且在
  任何寫入（blob_put／write）之前拒絕；內容位元組完全保留；`init` 重新啟用即復原
- CLI：`list` 文字模式顯示同樣資訊（`--json` 陣列格式不變）；`disable` 子指令；
  停用中的寫入指令被拒；`init --remote` 重新啟用
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import task_format as tf
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect

from .conftest import add_vault
from .test_tasks_archive_mcp import PathRecorder
from .test_tasks_cli_remote import AppClient, Cli
from .test_tasks_mcp import (
    MAIN_SPEC,
    MODE_HTTP,
    VAULT,
    Tasks,
    client_for,
    make_app,
    make_shell,
)

OTHER = "folder/other"
WRITE_PATHS = {"/v1/blob_put", "/v1/write", "/v1/update", "/v1/delete"}


def put_index(db_path: Path, data: dict, vault: str = VAULT) -> None:
    conn = connect(db_path)
    try:
        storage_sidecar.put(conn, vault, tf.INDEX_KEY, tf.encode(data), space="dev")
    finally:
        conn.close()


def task_blobs(db_path: Path) -> dict[str, tuple[int, bytes]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, version, content FROM sidecar_blobs"
            " WHERE vault = ? ORDER BY key",
            (VAULT,),
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: (r[1], bytes(r[2])) for r in rows if r[0] != tf.INDEX_KEY}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    path.mkdir()
    return path


@pytest.fixture
def app(db_path):
    add_vault(db_path, VAULT)
    add_vault(db_path, OTHER)
    return make_app(db_path)


# ── list：enabled／local_initialized ────────────────────────────────


@pytest.mark.anyio
async def test_list_reports_enabled_and_local_state(app, db_path, project):
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(app, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        # 尚未啟用：enabled false、沒有 next_step（不主動 init）
        listed = await stdio.ok(action="list")
        assert listed["enabled"] is False and listed["disabled"] is None
        assert listed["local_initialized"] is False and "next_step" not in listed
        # 服務端啟用（UI／HTTP init 只建索引）：stdio 提示 init
        await http.ok(action="init", vault=VAULT)
        listed = await stdio.ok(action="list")
        assert listed["enabled"] is True and listed["local_initialized"] is False
        assert "init" in listed["next_step"]
        assert listed["changes"] == []
        # HTTP 沒有本機欄位
        remote = await http.ok(action="list", vault=VAULT)
        assert remote["enabled"] is True
        assert "local_initialized" not in remote and "next_step" not in remote
        # stdio init 之後本機已初始化
        await stdio.ok(action="init")
        listed = await stdio.ok(action="list")
        assert listed["local_initialized"] is True and "next_step" not in listed
        assert (project / "openspec" / "config.yaml").read_text(encoding="utf-8").count(
            "remote: true"
        ) == 1


@pytest.mark.anyio
async def test_local_initialized_needs_remote_marker(app, db_path, project):
    """本機有 openspec/ 但沒標 remote: true 仍算未初始化（init 會補標記）。"""
    put_index(db_path, tf.empty_index())
    (project / "openspec").mkdir()
    (project / "openspec" / "config.yaml").write_text("schema: x\n", encoding="utf-8")
    async with client_for(make_shell(app, project)) as sc:
        listed = await Tasks(sc).ok(action="list")
    assert listed["local_initialized"] is False and "next_step" in listed


@pytest.mark.anyio
async def test_list_other_vault_does_not_suggest_local_init(app, db_path, project):
    put_index(db_path, tf.empty_index(), vault=OTHER)
    async with client_for(make_shell(app, project)) as sc:
        listed = await Tasks(sc).ok(action="list", vault=OTHER)
    assert listed["enabled"] is True and listed["local_initialized"] is False
    assert "next_step" not in listed and listed["local_reason"]


# ── disable ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_disable_blocks_actions_before_writes_and_init_restores(db_path, project):
    add_vault(db_path, VAULT)
    app = make_app(db_path)
    recorder = PathRecorder(app)
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(recorder, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await stdio.ok(action="propose", name="c1")
        frozen = task_blobs(db_path)

        first = await stdio.ok(action="disable", author="艾斯維爾")
        assert first["changed"] is True
        assert first["disabled"] == {"at": "2026-10-08T12:00:00Z", "by": "艾斯維爾"}
        again = await http.ok(action="disable", vault=VAULT)
        assert again["changed"] is False and again["disabled"] == first["disabled"]

        listed = await stdio.ok(action="list")
        assert listed["enabled"] is False
        assert listed["disabled"] == first["disabled"]
        assert [c["name"] for c in listed["changes"]] == ["c1"]

        cases = [
            {"action": "propose", "name": "c2"},
            {"action": "edit", "name": "c1", "expected_version": 1, "goal": "x"},
            {"action": "pull", "name": "c1"},
            {"action": "validate"},
            {"action": "archive", "name": "c1"},
        ]
        for case in cases:
            recorder.paths.clear()
            err = await http.err(vault=VAULT, **case)
            assert err["error"]["code"] == "tasks_disabled", case
            assert "重新啟用" in err["hint"]
            assert not WRITE_PATHS & set(recorder.paths), (case, recorder.paths)
        for case in [*cases, {"action": "sync_specs"}]:
            err = await stdio.err(**case)
            assert err["error"]["code"] == "tasks_disabled", case
        # 內容位元組完全保留（索引以外）
        assert task_blobs(db_path) == frozen

        restored = await stdio.ok(action="init")
        assert restored["created"] is False and restored["reenabled"] is True
        assert (await stdio.ok(action="list"))["enabled"] is True
        await stdio.ok(action="propose", name="c2")
    for key, value in frozen.items():
        if key != "tasks-snapshot":
            assert task_blobs(db_path)[key] == value, key


@pytest.mark.anyio
async def test_disable_requires_enabled_vault(app, project):
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        err = await Tasks(hc).err(action="disable", vault=VAULT)
    assert err["error"]["code"] == "tasks_not_enabled"


# ── CLI ─────────────────────────────────────────────────────────────


@pytest.fixture
def cli_remote(db_path, tmp_path, monkeypatch):
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    spec = path / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(MAIN_SPEC.encode("utf-8"))
    (path / "openspec" / "config.yaml").write_bytes(b"schema: spec-driven\n")
    add_vault(db_path, VAULT)
    with TestClient(make_app(db_path)) as http:
        yield Cli(path, AppClient(http))


def test_cli_list_shows_layer_state_and_disable_round_trip(cli_remote: Cli, db_path):
    c = cli_remote
    out = c.ok("list")
    assert "任務層：服務端未啟用；本機未標記 remote: true" in out
    put_index(db_path, tf.empty_index())
    out = c.ok("list")
    assert "服務端已啟用" in out and "init --remote" in out
    # --json 仍是陣列（stdout 格式不變）
    assert json.loads(c.ok("list", "--json")) == []

    c.ok("init", "--remote")
    c.ok("propose", "c1", "--skip-specs")
    assert "本機已初始化" in c.ok("list")
    frozen = task_blobs(db_path)

    out = c.ok("disable", "--by", "艾斯維爾")
    assert "已停用" in out and "艾斯維爾" in out
    assert "原本就已停用" in c.ok("disable")
    assert "已停用（艾斯維爾" in c.ok("list")

    code, out = c.run("propose", "c2", "--skip-specs")
    assert code != 0 and "已停用" in out, out
    assert not (c.root / "changes" / "c2").exists()
    code, out = c.run("validate")
    assert code != 0 and "已停用" in out, out
    code, out = c.run("push", "c1")
    assert code != 0 and "已停用" in out, out
    assert task_blobs(db_path) == frozen

    out = c.ok("init", "--remote")
    assert "已重新啟用" in out
    c.ok("propose", "c2", "--skip-specs")


def test_cli_disable_needs_enabled_vault(cli_remote: Cli):
    code, out = cli_remote.run("disable")
    assert code != 0 and "尚未啟用" in out
