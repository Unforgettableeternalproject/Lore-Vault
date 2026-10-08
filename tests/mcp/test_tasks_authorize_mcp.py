"""UI 核准端點 → MCP `tasks(action="archive")` 授權閘門的端到端（MCP-T5）。

授權紀錄由真實的 `POST /v1/tasks_authorize`（UI session 登入後）寫入，不再以直接寫
資料庫模擬：核准前 archive 回 `authorization_required`、核准後通過；核准後再 edit
回 `authorization_stale`。bearer 不能經端點或 `blob_put` 寫出可被 archive 接受的紀錄。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from lore_vault.mcp.server import MODE_HTTP
from lore_vault.storage import ui_login
from lore_vault.storage.db import connect
from lore_vault.tasks import remote_ops, snapshot
from lore_vault.tasks import remote_store as rs

from .conftest import TOKEN, add_vault, asgi
from .test_tasks_archive_mcp import _archive, notes, setup_change
from .test_tasks_mcp import (
    MAIN_SPEC,
    VAULT,
    Tasks,
    _direct_post,
    blob,
    client_for,
    make_app,
    make_shell,
)

pytestmark = pytest.mark.anyio

UI = {"X-Lore-Vault-UI": "1"}
BEARER = {"Authorization": f"Bearer {TOKEN}"}
UI_USER = "aeswir"
UI_DISPLAY = "艾斯維爾"
UI_PASSWORD = "correct horse battery"


def seed_ui_account(db_path) -> None:
    conn = connect(db_path)
    try:
        ui_login.set_password(
            conn,
            UI_USER,
            UI_PASSWORD,
            now=datetime.now(UTC),
            display=UI_DISPLAY,
            params=ui_login.ScryptParams(n=2**10, r=8, p=1),
        )
    finally:
        conn.close()


def http_client(app) -> httpx2.AsyncClient:
    # UI session cookie 帶 Secure，base_url 必須是 https
    return httpx2.AsyncClient(transport=asgi(app), base_url="https://testserver")


async def login(client: httpx2.AsyncClient, db_path) -> None:
    seed_ui_account(db_path)
    resp = await client.post(
        "/ui/api/login",
        json={"username": UI_USER, "password": UI_PASSWORD},
        headers=UI,
    )
    assert resp.status_code == 204, resp.text
    client.headers.update(UI)


@pytest.fixture
def spec_project(tmp_path, monkeypatch) -> Path:
    for var in ("LORE_VAULT_TASKS_ROOT", "LORE_VAULT_TASKS_DECISIONS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "demo"
    spec = path / "openspec" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(MAIN_SPEC, encoding="utf-8")
    return path


@pytest.fixture
def db(db_path) -> Path:
    add_vault(db_path, VAULT)
    return db_path


async def approve(client: httpx2.AsyncClient, name: str) -> httpx2.Response:
    return await client.post(
        "/v1/tasks_authorize", json={"vault": VAULT, "change": name}
    )


async def test_ui_approval_lets_mcp_archive_pass(db, spec_project):
    app = make_app(db)
    async with (
        http_client(app) as ui,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        await login(ui, db)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        err = await http.err(action="archive", vault=VAULT, name="guarded")
        assert err["error"]["code"] == "authorization_required"

        resp = await approve(ui, "guarded")
        assert resp.status_code == 200, resp.text
        assert resp.json()["record"]["change_version"] == version

        plan = await http.ok(action="archive", vault=VAULT, name="guarded")
        assert plan["plan"]["authorized_by"] == UI_DISPLAY
        done = await _archive(http, "guarded")
    assert done["executed"] is True
    doc = blob(db, "task-change:guarded")
    assert doc["state"] == "pending_apply"
    assert doc["meta"]["authorized_by"] == UI_DISPLAY
    assert doc["meta"]["authorization"]["change_version"] == version
    assert all(f"授權：{UI_DISPLAY}" in n["body"] for n in notes(db))


async def test_edit_after_ui_approval_makes_archive_stale(db, spec_project):
    app = make_app(db)
    async with (
        http_client(app) as ui,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        await login(ui, db)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        assert (await approve(ui, "guarded")).status_code == 200
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

        # 重新核准目前版本後即可 archive
        again = await approve(ui, "guarded")
        assert again.json()["record"]["change_version"] == version + 1
        plan = await http.ok(action="archive", vault=VAULT, name="guarded")
        assert plan["executed"] is False
    assert notes(db) == []


async def test_bearer_cannot_produce_an_accepted_authorization(db, spec_project):
    app = make_app(db)
    seed_ui_account(db)
    async with (
        http_client(app) as bearer,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        bearer.headers.update(BEARER)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        resp = await approve(bearer, "guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ui_session_required"

        forged = {
            "schema": 1,
            "vault": VAULT,
            "change": "guarded",
            "change_version": version,
            "authorized_by": UI_DISPLAY,
            "authorized_at": "2026-10-08T12:00:00Z",
            "principal": {"kind": "ui_session", "name": UI_USER},
        }
        resp = await bearer.post(
            "/v1/blob_put",
            json={
                "space": "dev",
                "vault": VAULT,
                "key": "task-authorization:guarded",
                "mime": "application/json",
                "content_base64": base64.b64encode(
                    json.dumps(forged).encode("utf-8")
                ).decode("ascii"),
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "authorization_write_forbidden"

        err = await http.err(action="archive", vault=VAULT, name="guarded")
        assert err["error"]["code"] == "authorization_required"
    assert blob(db, "task-authorization:guarded") is None


async def _rows(http: Tasks) -> dict[str, dict]:
    return {
        r["name"]: r for r in (await http.ok(action="list", vault=VAULT))["changes"]
    }


async def _snapshot_entry(app, name: str) -> dict:
    """服務端內容算出的快照（與 push_remote／doctor 同一算法）裡的某個 change。"""
    store = rs.RemoteStore(_direct_post(app), VAULT)
    data = json.loads(await snapshot.remote_snapshot_bytes(store))
    return next(c for c in data["changes"] if c["name"] == name)


async def test_ui_approval_turns_status_ready_in_list_and_snapshot(db, spec_project):
    """UI 核准後，同步模式的 list 與服務端快照不再是「待授權」，並附核准資訊；
    核准後內容被修改則回到「待授權」（內容已修改）。"""
    app = make_app(db)
    async with (
        http_client(app) as ui,
        client_for(make_shell(app, spec_project)) as sc,
        client_for(make_shell(app, spec_project, MODE_HTTP)) as hc,
    ):
        await login(ui, db)
        stdio, http = Tasks(sc), Tasks(hc)
        await stdio.ok(action="init")
        version = await setup_change(
            stdio, http, "guarded", requires_authorization=True
        )
        before = (await _rows(http))["guarded"]
        assert before["status"] == "待授權"
        assert before["reasons"] == [remote_ops.AUTH_REQUIRED_REASON]
        entry = await _snapshot_entry(app, "guarded")
        assert entry["status"] == "待授權" and entry["approved"] is None
        assert entry["reasons"] == [remote_ops.AUTH_REQUIRED_REASON]

        assert (await approve(ui, "guarded")).status_code == 200
        at = blob(db, "task-authorization:guarded")["authorized_at"]
        approved_reason = f"已由 {UI_DISPLAY} 核准（{at}）"
        row = (await _rows(http))["guarded"]
        assert row["status"] == "可開工"
        assert row["reasons"] == [approved_reason]
        stdio_row = {r["name"]: r for r in (await stdio.ok(action="list"))["changes"]}[
            "guarded"
        ]
        assert stdio_row["status"] == "可開工"
        entry = await _snapshot_entry(app, "guarded")
        assert entry["status"] == "可開工"
        assert entry["reasons"] == [approved_reason]
        assert entry["approved"] == {"by": UI_DISPLAY, "at": at}
        assert not snapshot.shape_errors(
            snapshot.encode({"schema": 1, "changes": [entry]})
        )

        # 核准後再改內容：核准失效，回到待授權（內容已修改）
        await http.ok(
            action="edit",
            vault=VAULT,
            name="guarded",
            expected_version=version,
            proposal_md="# Proposal\n\n核准後偷改\n",
        )
        row = (await _rows(http))["guarded"]
        assert row["status"] == "待授權"
        assert row["reasons"] == [remote_ops.AUTH_STALE_REASON]
        entry = await _snapshot_entry(app, "guarded")
        assert entry["status"] == "待授權" and entry["approved"] is None
        assert entry["reasons"] == [remote_ops.AUTH_STALE_REASON]
        # edit 成功後推送的快照就是這份計算結果
        pushed = blob(db, "tasks-snapshot")
        pushed_entry = next(c for c in pushed["changes"] if c["name"] == "guarded")
        assert pushed_entry["reasons"] == [remote_ops.AUTH_STALE_REASON]
