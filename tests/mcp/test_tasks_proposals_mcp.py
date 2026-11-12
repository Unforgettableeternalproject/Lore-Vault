"""`tasks(action="archive")` 與待審提案（add-agent-review-proposals）的協調。

- 規劃（第一步）登記 `task_archive` 提案並附在回應；仍不寫任何 note
- 兩個同意通道互斥：對話 token（claim pending）與 UI 核准（claim approved），
  同一份同意只會執行一次，輸的通道收到 `proposal_resolved`
- UI 先核准同一份規劃後，agent 再呼叫 archive（不帶 token）即視同同意直接執行
- 殼比服務新（服務沒有 /v1/proposal_register）時明確報錯、不寫入任何東西
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lore_vault.storage.db import connect

from .conftest import add_vault
from .test_tasks_archive_mcp import _archive, notes, setup_change
from .test_tasks_authorize_mcp import BEARER, http_client, login
from .test_tasks_mcp import (
    MAIN_SPEC,
    MODE_HTTP,
    VAULT,
    Tasks,
    client_for,
    make_app,
    make_shell,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def spec_project(tmp_path, monkeypatch):
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    spec = path / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(MAIN_SPEC, encoding="utf-8")
    return path


@pytest.fixture
def db(db_path):
    add_vault(db_path, VAULT)
    return db_path


def proposals(db_path, **where) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM review_proposals"
        if where:
            sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        return [dict(r) for r in conn.execute(sql, tuple(where.values()))]
    finally:
        conn.close()


async def test_plan_registers_proposal_and_token_path_closes_it(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        plan = await http.ok(action="archive", vault=VAULT, name="move-root")
        assert plan["executed"] is False and plan["proposal_pending"] is True
        prop = plan["proposal"]
        assert prop["kind"] == "task_archive" and prop["state"] == "pending"
        assert "payload" not in prop and "UI 審核頁" in plan["next_step"]
        assert notes(db) == []  # 規劃仍不寫入
        (row,) = proposals(db)
        assert row["id"] == prop["id"] and row["target_id"] == "move-root"
        # 重新規劃：命中同一提案
        again = await http.ok(action="archive", vault=VAULT, name="move-root")
        assert again["proposal"]["id"] == prop["id"] and again["proposal"]["deduped"]
        done = await http.ok(
            action="archive",
            vault=VAULT,
            name="move-root",
            confirm_token=again["confirm_token"],
        )
        assert done["executed"] is True
        assert done["proposal"] == {
            "id": prop["id"],
            "kind": "task_archive",
            "state": "applied",
            "resolved_via": "token",
        }
        assert proposals(db)[0]["state"] == "applied"
        assert proposals(db)[0]["claim_id"] is None


async def test_ui_approval_lets_agent_archive_without_token(db, spec_project):
    app = make_app(db)
    async with (
        http_client(app) as ui,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        await login(ui, db)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        plan = await http.ok(action="archive", vault=VAULT, name="move-root")
        prop = plan["proposal"]
        resp = await ui.post(
            "/v1/review/proposal_approve",
            json={"space": "dev", "vault": VAULT, "id": prop["id"], "expected_rev": 1},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["proposal"]["state"] == "approved"
        assert notes(db) == []  # UI 核准只是同意，不執行
        done = await http.ok(action="archive", vault=VAULT, name="move-root")
        assert done["executed"] is True and done["state"] == "pending_apply"
        assert done["proposal"]["resolved_via"] == "ui"
        assert [n["title"] for n in notes(db)][0].startswith("req:")
        row = proposals(db)[0]
        assert (row["state"], row["resolved_via"]) == ("applied", "ui")
        # 已封存：再呼叫不會重複執行
        again = await http.err(action="archive", vault=VAULT, name="move-root")
        assert again["error"]["code"] == "change_not_active"


async def test_only_one_consent_executes_when_channels_race(db, spec_project):
    app = make_app(db)
    async with (
        http_client(app) as bearer,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        bearer.headers.update(BEARER)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        plan = await http.ok(action="archive", vault=VAULT, name="move-root")
        # 另一個通道先拿到租約
        digest = json.loads(proposals(db)[0]["payload"])["plan_digest"]
        claimed = await bearer.post(
            "/v1/proposal_claim",
            json={
                "vault": VAULT,
                "id": plan["proposal"]["id"],
                "expect": "pending",
                "plan_digest": digest,
            },
        )
        assert claimed.status_code == 200, claimed.text
        lost = await http.err(
            action="archive",
            vault=VAULT,
            name="move-root",
            confirm_token=plan["confirm_token"],
        )
        assert lost["error"]["code"] in ("proposal_resolved", "proposal_executing")
        assert notes(db) == []  # 輸的通道沒有執行任何寫入


async def test_failed_execution_releases_the_lease(db, spec_project, monkeypatch):
    from lore_vault.tasks import remote_ops

    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        plan = await http.ok(action="archive", vault=VAULT, name="move-root")

        async def boom(*a, **k):
            raise RuntimeError("寫到一半失敗")

        monkeypatch.setattr(remote_ops, "archive_execute", boom)
        await http.err(
            action="archive",
            vault=VAULT,
            name="move-root",
            confirm_token=plan["confirm_token"],
        )
        row = proposals(db)[0]
        assert row["state"] == "pending" and row["claim_id"] is None


async def test_old_service_without_register_endpoint_is_an_explicit_error(
    db, spec_project
):
    inner = make_app(db)

    async def old_service(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/v1/proposal_register":
            body = json.dumps({"detail": "Not Found"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await inner(scope, receive, send)

    async with (
        client_for(make_shell(inner, spec_project)) as sc,
        client_for(make_shell(old_service, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        err = await http.err(action="archive", vault=VAULT, name="move-root")
        assert err["error"]["code"] == "service_outdated"
        assert notes(db) == []


async def test_same_plan_digest_updates_payload_in_place(db, spec_project):
    """同意綁 plan_digest：規劃相同、只有 allow_incomplete 等欄位不同時，
    就地更新提案（保留狀態），不是另開一筆。"""
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        first = await http.ok(action="archive", vault=VAULT, name="move-root")
        second = await http.ok(
            action="archive", vault=VAULT, name="move-root", allow_incomplete=True
        )
        assert second["proposal"]["id"] == first["proposal"]["id"]
        (row,) = proposals(db)
        assert json.loads(row["payload"])["allow_incomplete"] is True


async def test_existing_two_step_flow_still_works(db, spec_project):
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        done = await _archive(http, "move-root")
        assert done["executed"] is True


async def test_shell_renews_lease_and_reports_close_failure(
    db, spec_project, monkeypatch
):
    """執行期間定期續租；結案失敗（租約已失效）不吞掉，明確回報在結果裡。"""
    from lore_vault.tasks import mcp_tools, remote_ops

    seen: list[str] = []
    real_post_for = mcp_tools._post_for

    def recording_post_for(shell):
        inner = real_post_for(shell)

        async def post(path, body):
            seen.append(path)
            if path == "/v1/proposal_close" and body.get("outcome") == "applied":
                raise mcp_tools.rs.RemoteError(
                    409, "租約已失效", {"error": {"code": "proposal_resolved"}}
                )
            return await inner(path, body)

        return post

    real_execute = remote_ops.archive_execute

    async def slow_execute(*a, **k):
        await asyncio.sleep(0.4)
        return await real_execute(*a, **k)

    monkeypatch.setattr(mcp_tools, "_post_for", recording_post_for)
    monkeypatch.setattr(remote_ops, "archive_execute", slow_execute)
    monkeypatch.setattr(mcp_tools, "RENEW_INTERVAL_SECONDS", 0.05)
    app = make_app(db)
    async with (
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        await setup_change(stdio, http, "move-root", goal="搬資料根")
        plan = await http.ok(action="archive", vault=VAULT, name="move-root")
        done = await http.ok(
            action="archive",
            vault=VAULT,
            name="move-root",
            confirm_token=plan["confirm_token"],
        )
    assert done["executed"] is True
    assert done["proposal"]["close_error"]["code"] == "proposal_resolved"
    assert "沒有成功結案" in done["warning"]
    assert seen.count("/v1/proposal_renew") >= 1
