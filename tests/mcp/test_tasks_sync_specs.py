"""`tasks(action="sync_specs")`（archive 段二落地）、DECISIONS 鏡像與服務端計算的
任務快照（TASK_LAYER_MCP §1.4、MCP-T6）。

- sync_specs 只限 stdio：HTTP 在任何服務呼叫之前回 `path_not_supported`
- 本機主 spec 與封存時的基準不一致（git 被改過）→ 拒絕、一個檔案都不寫
- write-ahead：寫完主 spec、還沒建封存記錄就中斷，續跑認得出已落地、不重複併入
- 多個待落地 change 依封存先後處理
- DECISIONS：本機有檔案以本機為準，沒有時用 `task-decisions` 鏡像；本機沒有檔案時
  不推空鏡像
- 快照：寫入類動作成功後以服務端內容重算推送（含 `state`）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect
from lore_vault.tasks import remote_store as rs
from lore_vault.tasks import snapshot

from .conftest import add_vault
from .test_tasks_archive_mcp import (
    DONE_TASKS,
    PathRecorder,
    _archive,
    notes,
    setup_change,
)
from .test_tasks_mcp import (
    DECISIONS,
    DELTA_ADDED,
    DELTA_MODIFIED,
    MAIN_SPEC,
    MODE_HTTP,
    VAULT,
    Tasks,
    _direct_post,
    blob,
    client_for,
    make_app,
    make_shell,
)

pytestmark = pytest.mark.anyio

ARCHIVE_DIR = "2026-10-08"


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    path.mkdir()
    return path


@pytest.fixture
def spec_project(project) -> Path:
    spec = project / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(MAIN_SPEC, encoding="utf-8")
    return project


@pytest.fixture
def db(db_path) -> Path:
    add_vault(db_path, VAULT)
    return db_path


def _spec(project: Path) -> Path:
    return project / "openspec" / "specs" / "demo" / "spec.md"


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _put_blob(db_path: Path, key: str, data: dict[str, Any]) -> None:
    conn = connect(db_path)
    try:
        storage_sidecar.put(
            conn,
            VAULT,
            key,
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            space="dev",
            mime="application/json",
        )
    finally:
        conn.close()


class Pair:
    """同一個服務接 stdio 殼（有工作目錄）與 HTTP 殼。"""

    def __init__(self, app, project: Path) -> None:
        self.stdio_client = client_for(make_shell(app, project))
        self.http_client = client_for(make_shell(app, project, MODE_HTTP))

    async def __aenter__(self) -> tuple[Tasks, Tasks]:
        sc = await self.stdio_client.__aenter__()
        hc = await self.http_client.__aenter__()
        return Tasks(sc), Tasks(hc)

    async def __aexit__(self, *exc: Any) -> None:
        await self.http_client.__aexit__(*exc)
        await self.stdio_client.__aexit__(*exc)


# ── sync_specs ──────────────────────────────────────────────────────


async def test_sync_specs_lands_pending_apply_change(db, spec_project):
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        await _archive(http, "move-root")
        # 段一只動服務端：本機主 spec 與工作副本都還是舊的
        assert "~/.demo2/" not in _read(_spec(spec_project))
        working = spec_project / "openspec" / "changes" / "move-root"
        assert working.is_dir()

        done = await stdio.ok(action="sync_specs")
        assert [r["name"] for r in done["results"]] == ["move-root"]
        result = done["results"][0]
        assert result["written"] == ["demo"] and result["already_applied"] == []
        doc = blob(db, "task-change:move-root")
        assert doc["state"] == "archived"
        assert doc["apply"]["applied_caps"] == ["demo"]
        assert "applying" not in doc["apply"]
        assert _read(_spec(spec_project)) == doc["apply"]["merged_specs"]["demo"]
        assert not working.exists()
        record = spec_project / "openspec" / "changes" / "archive"
        record = record / f"{ARCHIVE_DIR}-move-root"
        meta = yaml.safe_load((record / ".openspec.yaml").read_text(encoding="utf-8"))
        assert meta["note_id"] == doc["meta"]["note_id"]
        assert meta["spec_applied"] is True
        assert _read(record / "specs" / "demo" / "spec.md") == DELTA_MODIFIED
        assert blob(db, "task-index")["changes"]["move-root"] == {"state": "archived"}
        # 快照由服務端內容計算：已落地 = 已完成
        entry = blob(db, "tasks-snapshot")["changes"][0]
        assert entry["state"] == "archived" and entry["status"] == "已完成"
        assert entry["note_id"] == doc["meta"]["note_id"]
        assert done["snapshot"]["pushed"] is True
        # 落地後鏡像就是本機現值
        assert done["mirrors"]["unchanged"] == ["demo"]

        again = await stdio.ok(action="sync_specs")
        assert again["results"] == []
        # depends_on 認得已落地的 change
        await http.ok(
            action="propose", vault=VAULT, name="next", depends_on=["move-root"]
        )
        rows = {
            r["name"]: r for r in (await http.ok(action="list", vault=VAULT))["changes"]
        }
        assert rows["next"]["status"] == "可開工" and "move-root" not in rows


async def test_sync_specs_http_refuses_before_any_service_call(db, project):
    recorder = PathRecorder(make_app(db))
    async with client_for(make_shell(recorder, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        recorder.paths.clear()
        err = await t.err(action="sync_specs", vault=VAULT)
        assert err["error"]["code"] == "path_not_supported"
        assert "stdio" in err["hint"]
        assert recorder.paths == []


async def test_sync_specs_rejects_when_local_base_changed(db, spec_project):
    """git 裡的主 spec 在封存後被別人改過：拒絕，不寫任何檔案、不搬目錄。"""
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root")
        await _archive(http, "move-root")
        edited = MAIN_SPEC.replace("示範。", "示範（別人改過）。")
        _spec(spec_project).write_bytes(edited.encode("utf-8"))

        err = await stdio.err(action="sync_specs", name="move-root")
        assert err["error"]["code"] == "spec_base_mismatch"
        assert err["error"]["capabilities"] == ["demo"]
        assert "還原" in err["hint"] or "合併" in err["hint"]
        assert _read(_spec(spec_project)) == edited
        assert (spec_project / "openspec" / "changes" / "move-root").is_dir()
        doc = blob(db, "task-change:move-root")
        assert doc["state"] == "pending_apply"
        assert "applied_caps" not in doc["apply"] and "applying" not in doc["apply"]


async def test_sync_specs_resumes_after_interrupt(db, spec_project, monkeypatch):
    """主 spec 已寫、封存記錄還沒建就中斷：續跑不重複併入（ADDED 重併會失敗）。"""
    real = rs.write_archive_record
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise rs.RemoteUnreachable("模擬落地中斷")
        return real(*args, **kwargs)

    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await setup_change(stdio, http, "add-export", deltas={"demo": DELTA_ADDED})
        await _archive(http, "add-export")
        monkeypatch.setattr(rs, "write_archive_record", flaky)

        err = await stdio.err(action="sync_specs")
        assert err["error"]["code"] == "service_unreachable"
        doc = blob(db, "task-change:add-export")
        assert doc["state"] == "pending_apply"
        assert doc["apply"]["applied_caps"] == ["demo"]
        assert "### Requirement: 匯出" in _read(_spec(spec_project))

        done = await stdio.ok(action="sync_specs")
        result = done["results"][0]
        assert result["written"] == [] and result["already_applied"] == ["demo"]
        assert result["archive_created"] is True
        assert _read(_spec(spec_project)).count("### Requirement: 匯出") == 1
        assert blob(db, "task-change:add-export")["state"] == "archived"


async def test_sync_specs_lands_in_archive_order(db, spec_project):
    """後封存的 change 併入結果疊在先封存的之上：必須依封存先後落地。"""
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await setup_change(stdio, http, "zz-first")
        await setup_change(stdio, http, "aa-second", deltas={"demo": DELTA_ADDED})
        await _archive(http, "zz-first")
        # 第二個封存時間較晚（測試用同一個 now，改 apply.archived_at 區分先後）
        await _archive(http, "aa-second")
        doc = blob(db, "task-change:aa-second")
        doc.pop("_version")
        doc["apply"]["archived_at"] = "2026-10-08T13:00:00Z"
        _put_blob(db, "task-change:aa-second", doc)

        done = await stdio.ok(action="sync_specs")
        assert [r["name"] for r in done["results"]] == ["zz-first", "aa-second"]
        text = _read(_spec(spec_project))
        assert "~/.demo2/" in text and "### Requirement: 匯出" in text


async def test_sync_specs_builds_record_from_server_without_working_copy(
    db, spec_project
):
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await http.ok(action="propose", vault=VAULT, name="remote-only", goal="g")
        await http.ok(
            action="edit",
            vault=VAULT,
            name="remote-only",
            expected_version=1,
            tasks_md=DONE_TASKS,
            deltas={"demo": DELTA_ADDED},
        )
        await http.ok(
            action="validate", vault=VAULT, name="remote-only", record_base=True
        )
        await _archive(http, "remote-only")
        assert not (spec_project / "openspec" / "changes" / "remote-only").exists()

        done = await stdio.ok(action="sync_specs")
        result = done["results"][0]
        assert result["working_copy_removed"] is False
        record = spec_project / "openspec" / "changes" / "archive"
        record = record / f"{ARCHIVE_DIR}-remote-only"
        assert (record / "tasks.md").read_text(encoding="utf-8") == DONE_TASKS
        assert _read(record / "specs" / "demo" / "spec.md") == DELTA_ADDED
        assert (record / "proposal.md").is_file()


async def test_sync_specs_refuses_unpushed_local_edits(db, spec_project):
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root")
        await stdio.ok(action="pull", name="move-root")
        await _archive(http, "move-root")
        tasks = spec_project / "openspec" / "changes" / "move-root" / "tasks.md"
        tasks.write_text("# Tasks\n\n- [x] 本機另外加的\n", encoding="utf-8")

        err = await stdio.err(action="sync_specs")
        assert err["error"]["code"] == "local_modified"
        assert "overwrite" in err["hint"]
        assert blob(db, "task-change:move-root")["state"] == "pending_apply"
        done = await stdio.ok(action="sync_specs", overwrite=True)
        assert done["results"][0]["working_copy_removed"] is True


# ── DECISIONS 鏡像 ──────────────────────────────────────────────────


def _with_decisions(project: Path) -> Path:
    (project / "DECISIONS.md").write_text(DECISIONS, encoding="utf-8")
    config = project / "openspec" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("decisions_file: DECISIONS.md\n", encoding="utf-8")
    return project / "DECISIONS.md"


async def test_decisions_mirror_lets_http_judge_blocked_by(db, project):
    path = _with_decisions(project)
    async with Pair(make_app(db), project) as (stdio, http):
        init = await stdio.ok(action="init")
        assert init["decisions"]["status"] == "pushed"
        mirror = blob(db, "task-decisions")
        assert mirror["decisions"] == {"D6": False, "D12": True}
        assert mirror["source_digest"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert set(mirror) == {"schema", "decisions", "source_digest", "_version"}
        # 同內容不重推
        again = await stdio.ok(action="validate")
        assert again["decisions"]["status"] == "unchanged"
        assert blob(db, "task-decisions")["_version"] == mirror["_version"]

        await http.ok(action="propose", vault=VAULT, name="blocked", blocked_by=["D6"])
        await http.ok(
            action="propose",
            vault=VAULT,
            name="cleared",
            blocked_by=["D12"],
            skip_specs=True,
        )
        rows = {
            r["name"]: r for r in (await http.ok(action="list", vault=VAULT))["changes"]
        }
        assert rows["blocked"]["status"] == "被擋住"
        assert rows["cleared"]["status"] == "可開工"
        # HTTP archive 也讀鏡像：不再是「無法判定」而拒絕
        await http.ok(
            action="edit",
            vault=VAULT,
            name="cleared",
            expected_version=1,
            tasks_md=DONE_TASKS,
        )
        done = await _archive(http, "cleared")
        assert done["state"] == "pending_apply"
    assert len(notes(db)) == 1


async def test_local_decisions_win_and_missing_file_never_pushes(db, project):
    path = _with_decisions(project)
    async with Pair(make_app(db), project) as (stdio, http):
        await stdio.ok(action="init")
        # 服務端鏡像過時（D6 被標成已解除），本機檔案仍說未解除
        _put_blob(
            db,
            rs.DECISIONS_KEY,
            {"schema": 1, "decisions": {"D6": True}, "source_digest": "old"},
        )
        await stdio.ok(action="propose", name="blocked", blocked_by=["D6"])
        local_rows = (await stdio.ok(action="list"))["changes"]
        assert local_rows[0]["status"] == "被擋住"
        http_rows = (await http.ok(action="list", vault=VAULT))["changes"]
        assert http_rows[0]["status"] == "可開工"

        # 本機沒有 DECISIONS.md：不推空鏡像、改用鏡像判定
        path.unlink()
        before = blob(db, rs.DECISIONS_KEY)["_version"]
        result = await stdio.ok(action="validate")
        assert result["decisions"]["status"] == "skipped"
        assert blob(db, rs.DECISIONS_KEY)["_version"] == before
        assert (await stdio.ok(action="list"))["changes"][0]["status"] == "可開工"


# ── 服務端計算的快照 ────────────────────────────────────────────────


async def test_mcp_writes_push_server_computed_snapshot(db, spec_project):
    async with Pair(make_app(db), spec_project) as (stdio, http):
        await stdio.ok(action="init")
        proposed = await http.ok(action="propose", vault=VAULT, name="c1", goal="g")
        assert proposed["snapshot"] == {"pushed": True, "changes": 1}
        entry = blob(db, "tasks-snapshot")["changes"][0]
        assert entry["name"] == "c1" and entry["state"] == "active"
        assert entry["tasks"] == {"done": 0, "total": 2}
        await http.ok(
            action="edit",
            vault=VAULT,
            name="c1",
            expected_version=1,
            tasks_md=DONE_TASKS,
            deltas={"demo": DELTA_MODIFIED},
        )
        entry = blob(db, "tasks-snapshot")["changes"][0]
        assert entry["tasks"] == {"done": 1, "total": 1}
        assert entry["specs"] == [
            {"capability": "demo", "requirement": "資料根目錄", "op": "MODIFIED"}
        ]
        await http.ok(action="validate", vault=VAULT, name="c1", record_base=True)
        await _archive(http, "c1")
        entry = blob(db, "tasks-snapshot")["changes"][0]
        assert entry["state"] == "pending_apply"
        assert entry["status"] == "已封存（待落地）"
        assert entry["note_id"] and entry["archived_at"] == "2026-10-08T12:00:00Z"

    # 推上去的內容就是服務端計算的結果，且符合 schema v1
    store = rs.RemoteStore(_direct_post(make_app(db)), VAULT)
    data = await snapshot.remote_snapshot_bytes(store)
    conn = connect(db)
    try:
        pushed = storage_sidecar.get(conn, VAULT, "tasks-snapshot", space="dev")
    finally:
        conn.close()
    assert pushed.content == data
    assert snapshot.shape_errors(data) == []


async def test_snapshot_lists_index_only_archived_names(db, project):
    """遷移的舊封存只登記在索引（沒有 change 文件）：快照仍列為已完成。"""
    store = rs.RemoteStore(_direct_post(make_app(db)), VAULT)
    await store.ensure_index()
    await store.set_index_state("legacy-old", rs.STATE_ARCHIVED)
    data = await snapshot.remote_snapshot_bytes(store)
    entry = json.loads(data)["changes"][0]
    assert entry["name"] == "legacy-old" and entry["status"] == "已完成"
    assert entry["state"] == "archived"
    assert snapshot.shape_errors(data) == []
