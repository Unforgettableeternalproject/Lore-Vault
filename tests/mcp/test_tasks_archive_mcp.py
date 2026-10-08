"""`tasks(action="archive")`：兩段式段一＋授權閘門
（TASK_LAYER_MCP §1.4、§3.3、MCP-T4）。

- 兩步式 confirm_token：第一步只規劃（不寫任何東西），第二步重算規劃、相符才執行
- 段一在服務端完成：寫 requirement／總結 note、主 spec 鏡像推進成併入後內容、
  change 狀態改 `pending_apply`（本機 specs 落地是 MCP-T6）
- `requires_authorization`：只認 UI 核准寫入的授權紀錄（`task-authorization:<name>`），
  工具參數沒有任何授權欄位；沒有紀錄時拒絕發生在讀 change 與授權紀錄之外的任何
  `/v1/*` 呼叫之前
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect
from lore_vault.tasks import mcp_tools
from lore_vault.tasks import remote_store as rs

from .conftest import add_vault
from .test_tasks_mcp import (
    DELTA_ADDED,
    DELTA_MODIFIED,
    MAIN_SPEC,
    MODE_HTTP,
    VAULT,
    Tasks,
    blob,
    client_for,
    make_app,
    make_shell,
)

pytestmark = pytest.mark.anyio

DONE_TASKS = "# Tasks\n\n- [x] 1.1 做完\n"
DELTA_ADDED_2 = DELTA_ADDED.replace("匯出", "匯入")


class PathRecorder:
    """記錄服務收到的每個請求路徑後原樣轉給 app。"""

    def __init__(self, app) -> None:
        self.app = app
        self.paths: list[str] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.paths.append(scope["path"])
        await self.app(scope, receive, send)


def put_authorization(
    db_path: Path,
    name: str,
    change_version: int,
    *,
    kind: str = "ui_session",
    by: str = "艾斯維爾",
) -> None:
    """模擬 MCP-T5 的 UI 核准端點寫入授權紀錄（本卡只讀）。"""
    record = {
        "schema": 1,
        "vault": VAULT,
        "change": name,
        "change_version": change_version,
        "authorized_by": by,
        "authorized_at": "2026-10-08T12:00:00Z",
        "principal": {"kind": kind, "name": "aeswir"},
    }
    conn = connect(db_path)
    try:
        storage_sidecar.put(
            conn,
            VAULT,
            rs.authorization_key(name),
            json.dumps(record, ensure_ascii=False).encode("utf-8"),
            space="dev",
            mime="application/json",
        )
    finally:
        conn.close()


def notes(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, body FROM notes WHERE vault = ? ORDER BY seq",
            (VAULT,),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "title": r[1], "body": r[2]} for r in rows]


async def setup_change(
    stdio: Tasks,
    http: Tasks,
    name: str,
    *,
    deltas: dict[str, str] | None = None,
    record: bool = True,
    **propose: Any,
) -> int:
    """建一個 tasks 全勾、帶 delta、記好 base 的 change；回傳目前版本。"""
    await stdio.ok(action="propose", name=name, **propose)
    edited = await http.ok(
        action="edit",
        vault=VAULT,
        name=name,
        expected_version=1,
        tasks_md=DONE_TASKS,
        deltas=deltas if deltas is not None else {"demo": DELTA_MODIFIED},
    )
    version = edited["version"]
    if record:
        result = await http.ok(
            action="validate", vault=VAULT, name=name, record_base=True
        )
        assert result["ok"], result
        version = result["results"][0]["version"]
    return version


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


async def _archive(t: Tasks, name: str, **extra: Any) -> dict[str, Any]:
    plan = await t.ok(action="archive", vault=VAULT, name=name, **extra)
    assert plan["executed"] is False
    return await t.ok(
        action="archive",
        vault=VAULT,
        name=name,
        confirm_token=plan["confirm_token"],
        **extra,
    )


# ── 兩步式＋段一 ────────────────────────────────────────────────────


async def test_archive_plan_then_execute_marks_pending_apply(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(stdio, http, "move-root", goal="搬資料根")

        plan = await http.ok(action="archive", vault=VAULT, name="move-root")
        assert plan["executed"] is False
        assert plan["plan"]["version"] == version
        assert plan["plan"]["requirements"] == ["demo/資料根目錄"]
        assert plan["plan"]["capabilities"] == ["demo"]
        assert "不要自動連打兩步" in plan["next_step"]
        # 第一步不寫任何東西
        assert notes(db) == []
        assert blob(db, "task-change:move-root")["_version"] == version

        done = await http.ok(
            action="archive",
            vault=VAULT,
            name="move-root",
            confirm_token=plan["confirm_token"],
        )
        assert done["executed"] is True
        assert done["state"] == "pending_apply"
        assert done["written"] == ["demo/資料根目錄", "summary"]
        titles = [n["title"] for n in notes(db)]
        assert titles == ["req:demo/資料根目錄", "變更 move-root：搬資料根"]
        assert done["note_id"] == notes(db)[1]["id"]

        doc = blob(db, "task-change:move-root")
        assert doc["state"] == "pending_apply"
        merged = doc["apply"]["merged_specs"]["demo"]
        assert "~/.demo2/" in merged and "~/.demo/`" not in merged
        assert doc["meta"]["note_id"] == done["note_id"]
        assert doc["meta"]["mirror_applied_caps"] == ["demo"]
        mirror = blob(db, "task-spec-mirror:demo")
        assert mirror["text"] == merged and mirror["source"] == "archive:move-root"
        assert doc["apply"]["mirror_versions"]["demo"] == mirror["_version"]
        assert blob(db, "task-index")["changes"]["move-root"] == {
            "state": "pending_apply"
        }

        listed = await http.ok(action="list", vault=VAULT)
        assert listed["changes"][0]["status"] == "已封存（待落地）"
        # pending_apply 不可再編輯、不可再 archive
        err = await http.err(
            action="edit",
            vault=VAULT,
            name="move-root",
            expected_version=doc["_version"],
            tasks_md="x",
        )
        assert err["error"]["code"] == "change_not_editable"
        err = await http.err(action="archive", vault=VAULT, name="move-root")
        assert err["error"]["code"] == "change_not_active"
        # stdio validate 不會用本機舊的主 spec 把推進過的鏡像倒退
        result = await stdio.ok(action="validate")
        assert "demo" in result["mirrors"]["skipped"]
        assert blob(db, "task-spec-mirror:demo")["text"] == merged


async def test_second_archive_merges_on_top_of_advanced_mirror(db, spec_project):
    """段一推進鏡像：第二個 change 在落地前 archive，併入結果保留第一個的修改。"""
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "first")
        await setup_change(stdio, http, "second", deltas={"demo": DELTA_ADDED})
        await _archive(http, "first")
        await _archive(http, "second")
    merged = blob(db, "task-change:second")["apply"]["merged_specs"]["demo"]
    assert "~/.demo2/" in merged and "### Requirement: 匯出" in merged


async def test_confirm_token_is_bound_to_args_and_plan(db, spec_project, monkeypatch):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(stdio, http, "c1")
        plan = await http.ok(action="archive", vault=VAULT, name="c1")
        token = plan["confirm_token"]
        # 參數不同（reason）→ invalid_confirm_token
        err = await http.err(
            action="archive", vault=VAULT, name="c1", reason="x", confirm_token=token
        )
        assert err["error"]["code"] == "invalid_confirm_token"
        err = await http.err(
            action="archive", vault=VAULT, name="c1", confirm_token="garbage"
        )
        assert err["error"]["code"] == "invalid_confirm_token"
        # 規劃後內容變了 → plan_changed（附新規劃與新 token，不執行）
        await http.ok(
            action="edit",
            vault=VAULT,
            name="c1",
            expected_version=version,
            proposal_md="# Proposal\n\n## Why\n\n改了\n",
        )
        err = await http.err(
            action="archive", vault=VAULT, name="c1", confirm_token=token
        )
        assert err["error"]["code"] == "plan_changed"
        assert err["error"]["plan"]["version"] == version + 1
        assert err["error"]["confirm_token"] != token
        assert notes(db) == []
        # 過期
        fresh = await http.ok(action="archive", vault=VAULT, name="c1")
        monkeypatch.setattr(mcp_tools, "_token_clock", lambda: 1e12)
        err = await http.err(
            action="archive",
            vault=VAULT,
            name="c1",
            confirm_token=fresh["confirm_token"],
        )
        assert err["error"]["code"] == "confirm_token_expired"
        assert notes(db) == []


async def test_archive_rejects_incomplete_unless_allowed(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(stdio, http, "c1")
        await http.ok(
            action="edit",
            vault=VAULT,
            name="c1",
            expected_version=version,
            tasks_md="- [x] a\n- [ ] b\n",
        )
        err = await http.err(action="archive", vault=VAULT, name="c1")
        assert err["error"]["code"] == "archive_rejected"
        assert "1/2" in err["error"]["message"]
        done = await _archive(http, "c1", allow_incomplete=True)
        assert done["executed"] is True
    assert blob(db, "task-change:c1")["meta"]["incomplete_at_archive"] == 1
    assert "封存時未完成：1 項" in notes(db)[-1]["body"]


async def test_archive_rejects_without_mirror_and_unknown_status(db, project):
    """HTTP 端：沒有鏡像不封存；讀不到 DECISIONS.md 的 blocked_by 不封存。"""
    app = make_app(db)
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="c1")
        await t.ok(
            action="edit",
            vault=VAULT,
            name="c1",
            expected_version=1,
            tasks_md=DONE_TASKS,
            deltas={"demo": DELTA_ADDED},
        )
        err = await t.err(action="archive", vault=VAULT, name="c1")
        assert err["error"]["code"] == "archive_rejected"
        assert "鏡像" in json.dumps(err, ensure_ascii=False)
        await t.ok(
            action="propose",
            vault=VAULT,
            name="pure",
            skip_specs=True,
            blocked_by=["D6"],
        )
        await t.ok(
            action="edit",
            vault=VAULT,
            name="pure",
            expected_version=1,
            tasks_md=DONE_TASKS,
        )
        err = await t.err(action="archive", vault=VAULT, name="pure")
        assert err["error"]["code"] == "archive_rejected"
        assert "無法判定" in err["error"]["message"]
    assert notes(db) == []


async def test_skip_specs_change_archives_without_mirror(db, project):
    app = make_app(db)
    async with client_for(make_shell(app, project, MODE_HTTP)) as hc:
        t = Tasks(hc)
        await t.ok(action="init", vault=VAULT)
        await t.ok(action="propose", vault=VAULT, name="pure", skip_specs=True)
        await t.ok(
            action="edit",
            vault=VAULT,
            name="pure",
            expected_version=1,
            tasks_md=DONE_TASKS,
        )
        done = await _archive(t, "pure")
    assert done["written"] == ["summary"] and done["capabilities"] == []
    assert blob(db, "task-change:pure")["apply"]["merged_specs"] == {}


async def test_archive_resumes_after_mirror_written_but_not_recorded(
    db, spec_project, monkeypatch
):
    """鏡像已推進、change 還沒記下就中斷：續跑以 write-ahead 雜湊認出，
    不當成 base 過時。"""
    app = make_app(db)
    real_put = rs.RemoteStore.put_mirror
    calls = {"n": 0}

    async def flaky(self, capability, **kw):
        version = await real_put(self, capability, **kw)
        if kw.get("source", "").startswith("archive:") and calls["n"] == 0:
            calls["n"] += 1
            raise rs.RemoteUnreachable("模擬推進鏡像後斷線")
        return version

    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "c1")
        monkeypatch.setattr(rs.RemoteStore, "put_mirror", flaky)
        plan = await http.ok(action="archive", vault=VAULT, name="c1")
        err = await http.err(
            action="archive",
            vault=VAULT,
            name="c1",
            confirm_token=plan["confirm_token"],
        )
        assert err["error"]["code"] == "service_unreachable"
        doc = blob(db, "task-change:c1")
        assert doc["state"] == "active" and "mirror_applying" in doc["meta"]
        before = len(notes(db))
        done = await _archive(http, "c1")
    assert done["executed"] is True and done["state"] == "pending_apply"
    assert done["written"] == [] and "summary" in done["skipped"]
    assert len(notes(db)) == before == 2


# ── 授權閘門 ────────────────────────────────────────────────────────


async def test_authorization_required_rejects_before_other_service_calls(
    db, spec_project
):
    app = make_app(db)
    recorder = PathRecorder(app)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(recorder, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "guarded", requires_authorization=True)
        recorder.paths.clear()
        err = await http.err(action="archive", vault=VAULT, name="guarded")
        assert err["error"]["code"] == "authorization_required"
        assert "UI" in err["hint"]
        # 只讀了 change 與授權紀錄；沒有 vault_resolve／list／write／blob_put
        assert recorder.paths == ["/v1/blob_get", "/v1/blob_get"]
        # 第二步（帶任意 token）同樣在閘門擋下
        recorder.paths.clear()
        err = await http.err(
            action="archive", vault=VAULT, name="guarded", confirm_token="x.y"
        )
        assert err["error"]["code"] in (
            "authorization_required",
            "invalid_confirm_token",
        )
        assert all(p == "/v1/blob_get" for p in recorder.paths)
    assert notes(db) == []


async def test_ui_authorization_record_allows_archive(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        put_authorization(db, "guarded", version)
        plan = await http.ok(action="archive", vault=VAULT, name="guarded")
        assert plan["plan"]["authorized_by"] == "艾斯維爾"
        done = await _archive(http, "guarded")
    assert done["executed"] is True
    doc = blob(db, "task-change:guarded")
    assert doc["meta"]["authorized_by"] == "艾斯維爾"
    assert doc["meta"]["authorization"]["change_version"] == version
    assert all("授權：艾斯維爾" in n["body"] for n in notes(db))


async def test_authorization_is_stale_after_edit(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        put_authorization(db, "guarded", version)
        await http.ok(
            action="edit",
            vault=VAULT,
            name="guarded",
            expected_version=version,
            proposal_md="# Proposal\n\n核准後偷改\n",
        )
        err = await http.err(action="archive", vault=VAULT, name="guarded")
    assert err["error"]["code"] == "authorization_stale"
    assert err["error"]["authorized_version"] == version
    assert err["error"]["current_version"] == version + 1
    assert notes(db) == []


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "bearer"},
        {"by": " "},
    ],
)
async def test_authorization_record_must_be_valid_ui_approval(db, spec_project, record):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        put_authorization(db, "guarded", version, **record)
        err = await http.err(action="archive", vault=VAULT, name="guarded")
    assert err["error"]["code"] == "authorization_required"
    assert notes(db) == []


async def test_tool_schema_has_no_authorization_input(app, project):
    """授權只能來自 UI 核准紀錄：tasks 工具的參數不得有任何「誰核准」的欄位。"""
    async with client_for(make_shell(app, project)) as client:
        tools = (await client.list_tools()).tools
    (tool,) = [t for t in tools if t.name == "tasks"]
    keys = set(tool.input_schema["properties"])
    assert "confirm_token" in keys and "requires_authorization" in keys
    for key in keys:
        lowered = key.lower()
        if "authoriz" in lowered or "approv" in lowered:
            assert key == "requires_authorization", key
    assert not keys & {"authorized_by", "authorization", "approved_by", "author_ized"}
