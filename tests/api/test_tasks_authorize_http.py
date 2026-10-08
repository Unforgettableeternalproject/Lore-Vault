"""`POST /v1/tasks_authorize` 與授權紀錄防偽（TASK_LAYER_MCP §3.3、MCP-T5）。

- 只允許 UI session：bearer 一律 403 `ui_session_required`（在 body 驗證之前）
- 授權人、principal、時間、版本由服務端填；請求自帶 422
- change 必須存在、active、`meta.requires_authorization` 為 true
- `/v1/blob_put` 對 `task-authorization:` 前綴不論認證方式一律 403
- 寫出的紀錄格式與任務層 `remote_store.parse_authorization` 相容
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import Config, EmbeddingConfig, TasksConfig, UiConfig
from lore_vault.tasks import remote_store as rs

from .conftest import (
    AUTH,
    DIM,
    UI_DISPLAY,
    UI_LOGIN,
    UI_USER,
    SpaceClient,
    make_settings,
    seed_ui_account,
)

UI = {"X-Lore-Vault-UI": "1"}
VAULT = "folder/tasks-auth"
ALIAS = "folder/tasks-auth-alias"
NAME = "guarded"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _config(**sections) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM), ui=UiConfig(static_dir=None), **sections
    )


def change_doc(name: str = NAME, *, state: str = "active", requires: bool = True):
    return {
        "schema": 1,
        "name": name,
        "state": state,
        "meta": {"requires_authorization": requires},
        "proposal_md": "# Proposal\n",
        "design_md": None,
        "tasks_md": "# Tasks\n\n- [x] 1.1 做完\n",
        "deltas": {},
        "apply": None,
    }


def put_change(bearer, doc: dict | bytes, name: str = NAME, **extra) -> int:
    content = doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")
    resp = bearer.post(
        "/v1/blob_put",
        json={
            "space": "dev",
            "vault": VAULT,
            "key": f"task-change:{name}",
            "mime": "application/json",
            "content_base64": b64(content),
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def get_record(client, name: str = NAME):
    return client.post(
        "/v1/blob_get",
        json={"space": "dev", "vault": VAULT, "key": f"task-authorization:{name}"},
    )


def authorize(client, **body):
    return client.post(
        "/v1/tasks_authorize", json={"vault": VAULT, "change": NAME, **body}
    )


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return body


@pytest.fixture
def open_client(db_path):
    """(bearer client, UI session client) 共用同一個 app；離開時關閉。"""
    opened: list[TestClient] = []

    def make(config: Config):
        seed_ui_account(db_path)
        app = create_app(make_settings(db_path, config=config))
        bearer = SpaceClient(app)
        bearer.__enter__()
        bearer.headers.update(AUTH)
        ui = TestClient(app, base_url="https://testserver")
        ui.__enter__()
        resp = ui.post("/ui/api/login", json=UI_LOGIN, headers=UI)
        assert resp.status_code == 204, resp.text
        ui.headers.update(UI)
        opened.extend([ui, bearer])
        return bearer, ui

    yield make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def clients(open_client):
    bearer, ui = open_client(_config())
    resp = bearer.post(
        "/v1/vaults", json={"key": VAULT, "display": VAULT, "aliases": [ALIAS]}
    )
    assert resp.status_code == 201, resp.text
    return bearer, ui


def test_ui_session_authorizes_current_version(clients):
    bearer, ui = clients
    put_change(bearer, change_doc())
    version = put_change(bearer, change_doc(), expected_version=1)
    assert version == 2
    _error(get_record(bearer), 404, "not_found")

    # 用別名也寫到正式 key；紀錄的 vault 是正式 key
    resp = ui.post("/v1/tasks_authorize", json={"vault": ALIAS, "change": NAME})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["created"] is True
    record = data["record"]
    assert record == {
        "schema": 1,
        "vault": VAULT,
        "change": NAME,
        "change_version": 2,
        "authorized_by": UI_DISPLAY,
        "authorized_at": record["authorized_at"],
        "principal": {"kind": "ui_session", "name": UI_USER},
    }
    # 秒級 UTC（`YYYY-MM-DDTHH:MM:SSZ`），不是毫秒格式
    assert len(record["authorized_at"]) == 20 and record["authorized_at"][-1] == "Z"

    stored = get_record(bearer)
    assert stored.status_code == 200
    blob = stored.json()
    assert blob["mime"] == "application/json"
    assert json.loads(base64.b64decode(blob["content_base64"])) == record
    # 任務層讀取端認得這份紀錄
    parsed = rs.parse_authorization(record, NAME)
    assert parsed.change_version == 2 and parsed.authorized_by == UI_DISPLAY

    # 同一版本再按一次：不重寫、保留原核准時間
    again = authorize(ui)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["record"] == record
    assert get_record(bearer).json()["version"] == blob["version"]

    # 核准後又 edit：再核准會覆寫成新版本
    put_change(bearer, change_doc(), expected_version=2)
    renewed = authorize(ui).json()
    assert renewed["created"] is True
    assert renewed["record"]["change_version"] == 3
    assert renewed["version"] == blob["version"] + 1


def test_bearer_is_rejected_before_body_validation(clients):
    bearer, _ = clients
    put_change(bearer, change_doc())
    _error(authorize(bearer), 403, "ui_session_required")
    no_body = bearer.post("/v1/tasks_authorize", json={"x": 1})
    _error(no_body, 403, "ui_session_required")
    _error(get_record(bearer), 404, "not_found")


@pytest.mark.parametrize(
    "extra",
    [
        {"principal": {"kind": "ui_session", "name": "x"}},
        {"authorized_by": "艾斯維爾"},
        {"authorized_at": "2026-10-08T00:00:00Z"},
        {"change_version": 1},
    ],
)
def test_request_cannot_supply_server_fields(clients, extra):
    bearer, ui = clients
    put_change(bearer, change_doc())
    assert authorize(ui, **extra).status_code == 422
    _error(get_record(bearer), 404, "not_found")


def test_rejects_changes_that_cannot_be_authorized(clients):
    bearer, ui = clients
    _error(authorize(ui), 404, "not_found")
    put_change(bearer, change_doc(requires=False))
    _error(authorize(ui), 409, "authorization_not_required")
    put_change(bearer, change_doc(state="pending_apply"))
    _error(authorize(ui), 409, "change_not_active")
    put_change(bearer, b"{not json")
    _error(authorize(ui), 409, "change_invalid")
    put_change(bearer, {**change_doc(), "schema": 2})
    _error(authorize(ui), 409, "change_invalid")
    _error(authorize(ui, space="lore"), 400, "invalid_request")
    _error(
        ui.post("/v1/tasks_authorize", json={"vault": VAULT, "change": "Bad Name"}),
        400,
        "invalid_request",
    )
    _error(
        ui.post("/v1/tasks_authorize", json={"vault": "folder/nope", "change": NAME}),
        404,
        "unknown_vault",
    )
    _error(get_record(bearer), 404, "not_found")


@pytest.mark.parametrize("who", ["bearer", "ui"])
def test_blob_put_cannot_forge_authorization_record(clients, who):
    bearer, ui = clients
    put_change(bearer, change_doc())
    client = bearer if who == "bearer" else ui
    forged = {
        "schema": 1,
        "vault": VAULT,
        "change": NAME,
        "change_version": 1,
        "authorized_by": "艾斯維爾",
        "authorized_at": "2026-10-08T00:00:00Z",
        "principal": {"kind": "ui_session", "name": "aeswir"},
    }
    resp = client.post(
        "/v1/blob_put",
        json={
            "space": "dev",
            "vault": VAULT,
            "key": f"task-authorization:{NAME}",
            "mime": "application/json",
            "content_base64": b64(json.dumps(forged).encode("utf-8")),
        },
    )
    _error(resp, 403, "authorization_write_forbidden")
    _error(get_record(bearer), 404, "not_found")


def test_remote_sync_off_rejects_authorization(open_client):
    bearer, ui = open_client(_config(tasks=TasksConfig(remote_sync=False)))
    resp = bearer.post("/v1/vaults", json={"key": VAULT, "display": VAULT})
    assert resp.status_code == 201, resp.text
    _error(authorize(ui), 403, "tasks_remote_sync_disabled")
