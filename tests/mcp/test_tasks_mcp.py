"""任務層 MCP 工具 `tasks(action=)`（TASK_LAYER_MCP §3、MCP-T3）。

- 真正的 `create_app` 經 ASGI transport 接到殼（不開網路埠、不連真實服務）
- 同一個服務同時接 stdio 殼（有工作目錄）與 HTTP 模式殼（沒有檔案系統），
  驗證兩種模式看到同一份服務端內容、HTTP 回應不含本機路徑
- 每項防護都有對應測試：版本衝突、授權欄位不可降級、任務層固定 dev space、
  本機未推送修改不被覆寫、鏡像缺失時不驗證
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, EmbeddingConfig, Secret, TasksConfig
from lore_vault.mcp import task_plugin
from lore_vault.mcp.server import MODE_HTTP, MODE_STDIO, Shell, build_server
from lore_vault.mcp.settings import ShellSettings
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect
from lore_vault.tasks import remote_store as rs

from .conftest import BASE_URL, DIM, TOKEN, NullEmbedder, Session, add_vault, asgi

pytestmark = pytest.mark.anyio

VAULT = "folder/demo"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

MAIN_SPEC = """# demo Specification

## Purpose
示範。

## Requirements

### Requirement: 資料根目錄
資料 SHALL 存放於 `~/.demo/`。

#### Scenario: 讀取資料根
- **WHEN** 解析路徑
- **THEN** 取得 `~/.demo/`
"""

DELTA_MODIFIED = """## MODIFIED Requirements

### Requirement: 資料根目錄
資料 SHALL 存放於 `~/.demo2/`。

#### Scenario: 讀取資料根
- **WHEN** 解析路徑
- **THEN** 取得 `~/.demo2/`
"""

DELTA_ADDED = """## ADDED Requirements

### Requirement: 匯出
系統 MUST 能匯出。

#### Scenario: 匯出
- **WHEN** 要求匯出
- **THEN** 產生檔案
"""

DECISIONS = """# 決策

### D6 對 U.E.P 的接口

**不在本次範圍。**

### D12 自架

**已裁決（艾斯維爾 2026-09-27）**：好。
"""


def make_app(db_path: Path, *, remote_sync: bool = True):
    return create_app(
        ApiSettings(
            db_path=db_path,
            snapshot_cache_dir=db_path.parent / "snapshot-cache",
            token=Secret(TOKEN),
            config=Config(
                embedding=EmbeddingConfig(dim=DIM),
                tasks=TasksConfig(remote_sync=remote_sync),
            ),
            query_embedder=NullEmbedder(),
            enrich_worker=False,
            embedding_warmup=False,
        )
    )


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    path.mkdir()
    return path


def make_shell(app, project: Path, mode: str = MODE_STDIO) -> Shell:
    return Shell(
        ShellSettings(
            base_url=BASE_URL,
            token=Secret(TOKEN),
            snapshot_on_start=False,
            timeout=5.0,
        ),
        transport=asgi(app),
        cwd=lambda: str(project),
        now=lambda: NOW,
        mode=mode,
    )


class Tasks:
    """`await t.ok(action=..., ...)`：呼叫 `tasks` 工具，回 payload。"""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.raw: list[str] = []

    async def call(self, **args) -> tuple[bool, dict[str, Any]]:
        # 不經 `Session.call`：它的第一個參數叫 name，會與 tasks 的 name 撞名
        result = await self.client.call_tool("tasks", args)
        text = result.content[0].text
        self.raw.append(text)
        if result.is_error:
            text = text[text.index("{") :]
        return bool(result.is_error), json.loads(text)

    async def ok(self, **args) -> dict[str, Any]:
        is_error, payload = await self.call(**args)
        assert not is_error, payload
        return payload

    async def err(self, **args) -> dict[str, Any]:
        is_error, payload = await self.call(**args)
        assert is_error, payload
        return payload


def client_for(shell: Shell) -> Client:
    server = build_server(shell)
    assert task_plugin.register(server, shell) == ["tasks"]
    return Client(server)


def assert_no_paths(texts: list[str], tmp_path: Path) -> None:
    forms = {
        str(tmp_path),
        str(tmp_path).replace("\\", "/"),
        str(tmp_path).replace("\\", "\\\\"),
    }
    for text in texts:
        for form in forms:
            assert form not in text, text
        assert "openspec/changes" not in text and "openspec\\\\changes" not in text


def blob(db_path: Path, key: str, space: str = "dev") -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        try:
            found = storage_sidecar.get(conn, VAULT, key, space=space)
        except Exception:  # noqa: BLE001 - not_found
            return None
        return json.loads(found.content.decode("utf-8")) | {"_version": found.version}
    finally:
        conn.close()


@pytest.fixture
def app(db_path):
    add_vault(db_path, VAULT)
    return make_app(db_path)


# ── init ────────────────────────────────────────────────────────────


async def test_stdio_init_creates_local_skeleton_gitignore_and_mirrors(
    db_path, project
):
    app = make_app(db_path)
    (project / ".git").mkdir()
    spec = project / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(MAIN_SPEC.replace("\n", "\r\n").encode("utf-8"))
    async with client_for(make_shell(app, project)) as client:
        t = Tasks(client)
        result = await t.ok(action="init", display="Demo")
        assert result["vault"] == VAULT and result["space"] == "dev"
        assert result["created"] is True
        local = result["local"]
        assert Path(local["root"]) == project / "openspec"
        assert "config.yaml" in local["created"]
        assert local["gitignore"] == {"status": "added", "entry": "openspec/changes/"}
        assert result["mirrors"]["pushed"] == ["demo"]
        # 重跑：不重建、不重複加 .gitignore、鏡像同內容不推
        again = await t.ok(action="init")
        assert again["created"] is False
        assert again["local"]["gitignore"]["status"] == "covered"
        assert again["mirrors"]["unchanged"] == ["demo"]
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("openspec/changes/") == 1
    mirror = blob(db_path, "task-spec-mirror:demo")
    # 鏡像原樣保存（CRLF 不被正規化）
    assert mirror["exists"] is True and "\r\n" in mirror["text"]
    assert mirror["_version"] == 1
    assert blob(db_path, "task-index")["changes"] == {}


async def test_init_gitignore_respects_broader_existing_rule(db_path, project):
    app = make_app(db_path)
    (project / ".gitignore").write_text("node_modules/\nopenspec/\n", encoding="utf-8")
    async with client_for(make_shell(app, project)) as client:
        result = await Tasks(client).ok(action="init", display="Demo")
    assert result["local"]["gitignore"]["status"] == "covered"
    text = (project / ".gitignore").read_text(encoding="utf-8")
    assert text == "node_modules/\nopenspec/\n"


async def test_http_init_requires_vault_and_returns_no_paths(
    app, db_path, project, tmp_path
):
    async with client_for(make_shell(app, project, MODE_HTTP)) as client:
        t = Tasks(client)
        err = await t.err(action="init")
        assert err["error"]["code"] == "vault_required"
        assert "remote_url" in err["hint"]
        result = await t.ok(action="init", vault=VAULT)
        assert result["created"] is True and "local" not in result
        assert "stdio" in result["note"]
        assert_no_paths(t.raw, tmp_path)
    # HTTP 模式完全不碰工作目錄
    assert not (project / "openspec").exists()
    assert not (project / ".gitignore").exists()


# ── propose → edit → pull（stdio 與 HTTP 一致）──────────────────────


async def test_propose_edit_pull_round_trip_across_modes(
    app, db_path, project, tmp_path
):
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(app, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        proposed = await stdio.ok(
            action="propose", name="add-export", goal="加上匯出", source="T-9"
        )
        assert proposed["version"] == 1 and proposed["state"] == "active"
        assert proposed["status"] == "可開工"
        assert proposed["local"]["written"] is True
        local_dir = project / "openspec" / "changes" / "add-export"
        assert (local_dir / "proposal.md").is_file()

        edited = await http.ok(
            action="edit",
            vault=VAULT,
            name="add-export",
            expected_version=1,
            proposal_md="# Proposal\n\n## Why\n\n要匯出。\n",
            deltas={"demo": DELTA_ADDED},
            design_md="# Design\n",
        )
        assert edited["version"] == 2
        assert edited["updated"] == ["deltas", "design_md", "proposal_md"]
        assert "local" not in edited

        pulled_http = await http.ok(action="pull", vault=VAULT, name="add-export")
        pulled_stdio = await stdio.ok(action="pull", name="add-export")
        assert pulled_http["change"] == pulled_stdio["change"]
        assert pulled_http["version"] == pulled_stdio["version"] == 2
        assert pulled_stdio["local"]["written"] is True
        assert pulled_stdio["local"]["previous"] == "behind"
        assert (local_dir / "specs" / "demo" / "spec.md").read_text(
            encoding="utf-8"
        ) == DELTA_ADDED
        assert (local_dir / "design.md").read_text(encoding="utf-8") == "# Design\n"
        assert pulled_http["change"]["meta"]["goal"] == "加上匯出"
        assert "remote_version" not in pulled_http["change"]["meta"]
        meta = (local_dir / ".openspec.yaml").read_text(encoding="utf-8")
        assert "remote_version: 2" in meta and "remote_digest:" in meta

        # stdio 的 edit 同步寫回本機（本機未改過）
        again = await stdio.ok(
            action="edit", name="add-export", expected_version=2, design_md=""
        )
        assert again["local"]["written"] is True
        assert not (local_dir / "design.md").exists()
        assert_no_paths(http.raw, tmp_path)


async def test_edit_with_stale_version_conflicts_and_returns_current(app, project):
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        await t.ok(
            action="edit", vault=VAULT, name="c1", expected_version=1, tasks_md="A"
        )
        err = await t.err(
            action="edit", vault=VAULT, name="c1", expected_version=1, tasks_md="B"
        )
    assert err["error"]["code"] == "version_conflict"
    assert err["error"]["expected"] == 1
    assert err["error"]["current"]["version"] == 2
    assert err["error"]["current"]["change"]["tasks_md"] == "A"
    assert "expected_version" in err["hint"]


async def test_edit_race_is_caught_by_service_version_lock(
    app, db_path, project, monkeypatch
):
    """讀到的版本已過期（另一方剛寫入）：工具端比對放行，服務端樂觀鎖仍擋下。"""
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        stale = await rs.RemoteStore(_direct_post(app), VAULT).require_change("c1")
        await t.ok(
            action="edit", vault=VAULT, name="c1", expected_version=1, tasks_md="A"
        )

        async def stale_copy(self, name):
            return rs.RemoteChange.from_doc(json.loads(json.dumps(stale.doc)), 1, VAULT)

        monkeypatch.setattr(rs.RemoteStore, "require_change", stale_copy)
        err = await t.err(
            action="edit", vault=VAULT, name="c1", expected_version=1, tasks_md="B"
        )
    assert err["error"]["code"] == "version_conflict"
    assert err["error"]["current"]["version"] == 2
    assert blob(db_path, "task-change:c1")["tasks_md"] == "A"


async def test_edit_cannot_downgrade_authorization(app, db_path, project):
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(
            action="propose", vault=VAULT, name="guarded", requires_authorization=True
        )
        err = await t.err(
            action="edit",
            vault=VAULT,
            name="guarded",
            expected_version=1,
            requires_authorization=False,
        )
        assert err["error"]["code"] == "authorization_downgrade_forbidden"
        # false → true 允許
        await t.ok(action="propose", vault=VAULT, name="open")
        up = await t.ok(
            action="edit",
            vault=VAULT,
            name="open",
            expected_version=1,
            requires_authorization=True,
        )
        assert up["version"] == 2
    assert blob(db_path, "task-change:guarded")["meta"]["requires_authorization"]


async def test_edit_rejects_archive_in_progress_and_empty_edit(app, db_path, project):
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        err = await t.err(action="edit", vault=VAULT, name="c1", expected_version=1)
        assert err["error"]["code"] == "no_changes"
        # 模擬 archive 已寫入部分 note
        store = rs.RemoteStore(_direct_post(app), VAULT)
        change = await store.require_change("c1")
        change.meta["notes"] = {"demo/x": "n1"}
        await store.save_change(change)
        err = await t.err(
            action="edit", vault=VAULT, name="c1", expected_version=2, tasks_md="x"
        )
        assert err["error"]["code"] == "archive_in_progress"


async def test_propose_rejects_duplicates_and_bad_input(app, project):
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        dup = await t.err(action="propose", vault=VAULT, name="c1")
        assert dup["error"]["code"] == "change_exists"
        bad = await t.err(action="propose", vault=VAULT, name="Bad_Name")
        assert bad["error"]["code"] == "invalid_name"
        bad = await t.err(action="propose", vault=VAULT, name="c2", blocked_by=["T-1"])
        assert bad["error"]["code"] == "invalid_request"
        unknown = await t.err(action="pull", vault=VAULT, name="nope")
        assert unknown["error"]["code"] == "change_not_found"
        action = await t.err(action="explode", vault=VAULT)
        assert action["error"]["code"] == "invalid_request"


async def test_tasks_always_use_dev_space(app, db_path, project):
    """MCP 目前 space 切到 lore 時，任務層照樣讀寫 dev（`_send` 會注入目前 space）。"""
    shell = make_shell(app, project)
    async with client_for(shell) as sc:
        session = Session(sc)
        await session.ok("space", action="set", value="lore")
        t = Tasks(sc)
        await t.ok(action="init")
        await t.ok(action="propose", name="c1")
        listed = await t.ok(action="list")
    assert [c["name"] for c in listed["changes"]] == ["c1"]
    assert blob(db_path, "task-change:c1")["name"] == "c1"


# ── pull：本機未推送修改 ─────────────────────────────────────────────


async def test_stdio_pull_refuses_to_overwrite_local_modifications(app, project):
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(app, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await stdio.ok(action="propose", name="c1")
        tasks_md = project / "openspec" / "changes" / "c1" / "tasks.md"
        tasks_md.write_text("本機改的\n", encoding="utf-8")
        await http.ok(
            action="edit", vault=VAULT, name="c1", expected_version=1, tasks_md="遠端"
        )
        err = await stdio.err(action="pull", name="c1")
        assert err["error"]["code"] == "local_modified"
        assert err["error"]["local_state"] == "diverged"
        assert "overwrite=true" in err["hint"]
        assert tasks_md.read_text(encoding="utf-8") == "本機改的\n"
        # stdio edit 也不覆寫本機修改
        edited = await stdio.ok(action="edit", name="c1", expected_version=2, goal="g")
        assert edited["local"]["written"] is False
        assert tasks_md.read_text(encoding="utf-8") == "本機改的\n"
        forced = await stdio.ok(action="pull", name="c1", overwrite=True)
        assert forced["local"]["written"] is True
        assert tasks_md.read_text(encoding="utf-8") == "遠端"
        # 同步後再 pull：in_sync
        again = await stdio.ok(action="pull", name="c1")
        assert again["local"]["previous"] == "in_sync"


async def test_stdio_explicit_other_vault_never_touches_local_files(
    app, db_path, project
):
    add_vault(db_path, "folder/other")
    async with client_for(make_shell(app, project)) as sc:
        t = Tasks(sc)
        await t.ok(action="init")
        await t.ok(action="init", vault="folder/other")
        proposed = await t.ok(action="propose", vault="folder/other", name="x1")
        assert proposed["local"]["written"] is False
        assert "binding" in proposed["local"]["reason"]
    assert not (project / "openspec" / "changes" / "x1").exists()


# ── list ────────────────────────────────────────────────────────────


async def test_list_statuses_stdio_reads_decisions_http_cannot(app, project, tmp_path):
    (project / "DECISIONS.md").write_text(DECISIONS, encoding="utf-8")
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(app, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        config = project / "openspec" / "config.yaml"
        config.write_text("decisions_file: DECISIONS.md\n", encoding="utf-8")
        await stdio.ok(action="propose", name="base")
        await stdio.ok(action="propose", name="needs-base", depends_on=["base"])
        await stdio.ok(action="propose", name="blocked", blocked_by=["D6"])
        await stdio.ok(action="propose", name="cleared", blocked_by=["D12"])
        await stdio.ok(action="propose", name="guarded", requires_authorization=True)
        rows = {r["name"]: r for r in (await stdio.ok(action="list"))["changes"]}
        assert rows["base"]["status"] == "可開工"
        assert rows["needs-base"]["status"] == "被擋住"
        assert rows["blocked"]["status"] == "被擋住"
        assert rows["cleared"]["status"] == "可開工"
        assert rows["guarded"]["status"] == "待授權"
        assert "UI" in rows["guarded"]["reasons"][0]
        assert rows["base"]["tasks"] == "0/2" and rows["base"]["version"] == 1

        http_rows = {
            r["name"]: r for r in (await http.ok(action="list", vault=VAULT))["changes"]
        }
        assert http_rows["blocked"]["status"] == "無法判定"
        assert http_rows["cleared"]["status"] == "無法判定"
        filtered = await http.ok(action="list", vault=VAULT, status_filter="待授權")
        assert [r["name"] for r in filtered["changes"]] == ["guarded"]
        everything = await http.ok(action="list", vault="*")
        assert {r["vault"] for r in everything["changes"]} == {VAULT}
        assert len(everything["changes"]) == 5
        assert_no_paths(http.raw, tmp_path)


# ── validate（主 spec 讀鏡像）───────────────────────────────────────


async def test_validate_uses_mirror_and_records_base(app, db_path, project, tmp_path):
    spec = project / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(MAIN_SPEC, encoding="utf-8")
    async with (
        client_for(make_shell(app, project)) as sc,
        client_for(make_shell(app, project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await stdio.ok(action="propose", name="move-root")
        await http.ok(
            action="edit",
            vault=VAULT,
            name="move-root",
            expected_version=1,
            deltas={"demo": DELTA_MODIFIED},
        )
        first = await http.ok(action="validate", vault=VAULT, name="move-root")
        assert first["ok"] is False
        assert any("base 未記錄" in e for e in first["results"][0]["errors"])
        recorded = await http.ok(
            action="validate", vault=VAULT, name="move-root", record_base=True
        )
        assert recorded["ok"] is True, recorded
        assert recorded["results"][0]["base_recorded"] == ["demo/資料根目錄"]
        assert recorded["results"][0]["version"] == 3
        # stdio validate：推鏡像（未變→unchanged）、附本機同步狀態
        local = await stdio.ok(action="validate")
        assert local["ok"] is True
        assert local["mirrors"]["unchanged"] == ["demo"]
        assert local["results"][0]["local_sync"] == "behind"
        # 主 spec 在 git 端被改：stdio validate 重推鏡像，base 過時被抓到
        spec.write_text(
            MAIN_SPEC.replace("~/.demo/`。", "`/srv/demo`。"), encoding="utf-8"
        )
        stale = await stdio.ok(action="validate", name="move-root")
        assert stale["mirrors"]["pushed"] == ["demo"]
        assert stale["ok"] is False
        assert any("base 過時" in e for e in stale["results"][0]["errors"])
        assert_no_paths(http.raw, tmp_path)


async def test_validate_without_mirror_reports_missing_instead_of_skeleton(
    app, project
):
    """沒有鏡像時不可把主 spec 當「不存在」：MODIFIED 會誤報、ADDED 會生 skeleton。"""
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        await t.ok(
            action="edit",
            vault=VAULT,
            name="c1",
            expected_version=1,
            deltas={"newcap": DELTA_ADDED},
        )
        result = await t.ok(action="validate", vault=VAULT, name="c1", record_base=True)
    row = result["results"][0]
    assert row["ok"] is False and "base_recorded" not in row
    assert any("鏡像" in e for e in row["errors"])


async def test_stdio_mirror_marks_new_capability_as_absent(app, db_path, project):
    async with client_for(make_shell(app, project)) as sc:
        t = Tasks(sc)
        await t.ok(action="init")
        await t.ok(action="propose", name="c1")
        await t.ok(
            action="edit", name="c1", expected_version=1, deltas={"newcap": DELTA_ADDED}
        )
        result = await t.ok(action="validate", name="c1", record_base=True)
    assert result["mirrors"]["pushed"] == ["newcap"]
    assert result["ok"] is True, result
    mirror = blob(db_path, "task-spec-mirror:newcap")
    assert mirror["exists"] is False and mirror["text"] is None


# ── 開關與 remote_store ─────────────────────────────────────────────


async def test_remote_sync_disabled_blocks_writes_not_reads(db_path, project):
    add_vault(db_path, VAULT)
    on = make_app(db_path)
    async with client_for(make_shell(on, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
    off = make_app(db_path, remote_sync=False)
    async with client_for(make_shell(off, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        err = await t.err(action="propose", vault=VAULT, name="c2")
        assert err["error"]["code"] == "tasks_remote_sync_disabled"
        assert "python -m lore_vault.tasks" in err["hint"]
        listed = await t.ok(action="list", vault=VAULT)
        assert [c["name"] for c in listed["changes"]] == ["c1"]
        pulled = await t.ok(action="pull", vault=VAULT, name="c1")
        assert pulled["version"] == 1


async def test_remote_store_never_writes_authorization_records(app):
    store = rs.RemoteStore(_direct_post(app), VAULT)
    with pytest.raises(rs.StoreError) as info:
        await store.put_blob(rs.authorization_key("c1"), b"{}", expected_version=None)
    assert info.value.code == "authorization_write_forbidden"


async def test_local_digest_round_trips_crlf_and_yaml(app, project):
    """寫回本機後重讀的內容雜湊與服務端一致（CRLF、日期字串都不走樣）。"""
    async with client_for(make_shell(app, project)) as sc:
        t = Tasks(sc)
        await t.ok(action="init")
        await t.ok(action="propose", name="c1", goal="g")
        await t.ok(
            action="edit",
            name="c1",
            expected_version=1,
            tasks_md="- [ ] a\r\n- [x] b\r\n",
            deltas={"demo": DELTA_ADDED.replace("\n", "\r\n")},
        )
        result = await t.ok(action="validate", name="c1")
    assert result["results"][0]["local_sync"] == "in_sync"


def _direct_post(app) -> rs.Post:
    """測試用：不經 MCP，直接以殼的 client 打服務（dev space）。"""
    shell = Shell(
        ShellSettings(base_url=BASE_URL, token=Secret(TOKEN), snapshot_on_start=False),
        transport=asgi(app),
    )
    from lore_vault.tasks.mcp_tools import _post_for

    return _post_for(shell)
