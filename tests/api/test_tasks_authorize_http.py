"""`POST /v1/tasks_authorize` 與授權紀錄防偽（TASK_LAYER_MCP §3.3、MCP-T5）。

- 只允許 UI session：bearer 一律 403 `ui_session_required`（在 body 驗證之前）
- 授權人、principal、時間、版本由服務端填；請求自帶 422
- change 必須存在、active、`meta.requires_authorization` 為 true
- `/v1/blob_put` 對 `task-authorization:` 前綴不論認證方式一律 403
- 寫出的紀錄格式與任務層 `remote_store.parse_authorization` 相容
- 紀錄綁定內容雜湊（`content_digest`）；`/v1/blob_put` 寫 `task-change:` 經授權守衛：
  需授權的 change 不可取消標記、不可不經核准就離開 active 或寫入 archive 簿記
  （審查列的直接 HTTP 攻擊路徑各一條測試）
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import task_format as tf
from lore_vault.api import tasks_admin
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


def post_change(bearer, doc: dict | bytes, name: str = NAME, **extra):
    content = doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")
    return bearer.post(
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


def put_change(bearer, doc: dict | bytes, name: str = NAME, **extra) -> int:
    resp = post_change(bearer, doc, name, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def stored_change(bearer, name: str = NAME) -> dict:
    resp = bearer.post(
        "/v1/blob_get",
        json={"space": "dev", "vault": VAULT, "key": f"task-change:{name}"},
    )
    assert resp.status_code == 200, resp.text
    return json.loads(base64.b64decode(resp.json()["content_base64"]))


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
        "content_digest": tf.authorization_digest(change_doc()),
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
    assert parsed.content_digest == record["content_digest"]

    # 同一內容再按一次：不重寫、保留原核准時間
    again = authorize(ui)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["record"] == record
    assert get_record(bearer).json()["version"] == blob["version"]

    # 版本前進但內容相同（例如同內容重推）：核准仍有效、不重寫
    put_change(bearer, change_doc(), expected_version=2)
    assert authorize(ui).json()["created"] is False

    # 核准後又改內容：再核准會覆寫成新內容
    edited = {**change_doc(), "tasks_md": "# Tasks\n\n- [x] 1.1 改過\n"}
    put_change(bearer, edited, expected_version=3)
    renewed = authorize(ui).json()
    assert renewed["created"] is True
    assert renewed["record"]["change_version"] == 4
    assert renewed["record"]["content_digest"] == tf.authorization_digest(edited)
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
    put_change(bearer, change_doc(state="pending_apply", requires=False))
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


# ── `/v1/blob_put` 的 task-change: 授權守衛（直接 HTTP 攻擊）──


def with_deltas(doc: dict) -> dict:
    return {**doc, "deltas": {"demo": "## ADDED Requirements\n\n### Requirement: X\n"}}


def archived_by_attacker(doc: dict, state: str = "pending_apply") -> dict:
    """攻擊者自己湊的段一結果：狀態、note、merged_specs 全部偽造。"""
    return {
        **doc,
        "state": state,
        "meta": {**doc["meta"], "note_id": "01FAKE", "notes": {"summary": "01FAKE"}},
        "apply": {
            "archived_at": "2026-10-09T00:00:00Z",
            "merged_specs": {"demo": "# demo\n\n偽造的主 spec\n"},
            "mirror_versions": {},
        },
    }


@pytest.mark.parametrize("approved", [False, True])
def test_attack_downgrade_requires_authorization(clients, approved):
    """攻擊 1：把 requires_authorization 改成 false 再走 archive（不論有沒有核准）。"""
    bearer, ui = clients
    put_change(bearer, change_doc())
    if approved:
        assert authorize(ui).status_code == 200
    downgraded = change_doc(requires=False)
    for extra in ({}, {"expected_version": 1}):
        _error(
            post_change(bearer, downgraded, **extra),
            403,
            "authorization_downgrade_forbidden",
        )
    # 連同其他欄位一起改、或整個 meta 拿掉也一樣
    _error(
        post_change(bearer, {**change_doc(), "meta": {}}),
        403,
        "authorization_downgrade_forbidden",
    )
    assert stored_change(bearer)["meta"]["requires_authorization"] is True


def test_attack_forge_pending_apply_without_authorization(clients):
    """攻擊 2：直接寫 state=pending_apply 並配好 deltas／merged_specs，跳過 archive。"""
    bearer, _ = clients
    put_change(bearer, change_doc())
    for state in ("pending_apply", "archived"):
        _error(
            post_change(bearer, archived_by_attacker(with_deltas(change_doc()), state)),
            403,
            "authorization_required",
        )
    assert stored_change(bearer)["state"] == "active"


def test_attack_forge_pending_apply_with_content_changed_after_approval(clients):
    """核准後換掉 delta 再偽造段一結果：紀錄雜湊對不上新內容，一樣 403。"""
    bearer, ui = clients
    put_change(bearer, change_doc())
    assert authorize(ui).status_code == 200
    _error(
        post_change(bearer, archived_by_attacker(with_deltas(change_doc()))),
        403,
        "authorization_required",
    )
    # 只動 tasks_md（不碰 delta）也算內容改變
    tampered = {**change_doc(), "tasks_md": "# Tasks\n\n- [x] 偷改\n"}
    _error(
        post_change(bearer, archived_by_attacker(tampered)),
        403,
        "authorization_required",
    )


def test_attack_create_change_directly_as_pending_apply(clients):
    """新建（expected_version=0）就是 pending_apply：需要紀錄，否則 403。"""
    bearer, _ = clients
    forged = archived_by_attacker(with_deltas(change_doc("sneaky")))
    forged["name"] = "sneaky"
    _error(
        post_change(bearer, forged, "sneaky", expected_version=0),
        403,
        "authorization_required",
    )
    _error(post_change(bearer, forged, "sneaky"), 403, "authorization_required")
    # archived 但沒有 note_id／legacy_archive 也不放行
    bare = {**change_doc("sneaky"), "state": "archived"}
    _error(post_change(bearer, bare, "sneaky"), 403, "authorization_required")


def test_migrated_archive_can_still_be_created(clients):
    """`tasks migrate` 遷入的本機舊封存：archived＋note_id（或 legacy_archive）
    可新建。"""
    bearer, _ = clients
    migrated = change_doc("old-one", state="archived")
    migrated["meta"] = {**migrated["meta"], "note_id": "01REAL", "authorized_by": "x"}
    assert put_change(bearer, migrated, "old-one", expected_version=0) == 1
    legacy = change_doc("legacy-one", state="archived")
    legacy["meta"] = {**legacy["meta"], "legacy_archive": "OpenSpec 試用"}
    assert put_change(bearer, legacy, "legacy-one", expected_version=0) == 1
    # 但已存在的需授權 archived 文件不能被改回 pending_apply
    _error(
        post_change(bearer, archived_by_attacker(migrated), "old-one"),
        403,
        "authorization_required",
    )


def test_attack_forge_cli_authorization_source(clients):
    """攻擊 4：meta.authorization 自稱 `source: "cli"`，伺服器不認。"""
    bearer, _ = clients
    put_change(bearer, change_doc())
    forged = archived_by_attacker(change_doc())
    forged["meta"]["authorization"] = {
        "authorized_by": "艾斯維爾",
        "authorized_at": "2026-10-09T00:00:00Z",
        "change_version": 1,
        "source": "cli",
    }
    forged["meta"]["authorized_by"] = "艾斯維爾"
    _error(post_change(bearer, forged), 403, "authorization_required")
    # active 狀態下偷塞 archive 簿記同樣要紀錄
    sneaky = change_doc()
    sneaky["meta"] = {
        **sneaky["meta"],
        "authorization": forged["meta"]["authorization"],
    }
    _error(post_change(bearer, sneaky), 403, "authorization_required")
    assert "authorization" not in stored_change(bearer)["meta"]


def test_approved_archive_bookkeeping_passes_guard(clients):
    """合法路徑：核准目前內容後，archive 段一／段二的簿記寫入（內容雜湊不變）都放行；
    一般 edit（不碰簿記）不需紀錄，但會讓核准失效。"""
    bearer, ui = clients
    put_change(bearer, change_doc())
    # 沒核准時的一般 edit：放行
    v = put_change(bearer, with_deltas(change_doc()), expected_version=1)
    assert authorize(ui).status_code == 200
    half = with_deltas(change_doc())
    half["meta"] = {**half["meta"], "vault": VAULT, "notes": {"summary": "01N"}}
    v = put_change(bearer, half, expected_version=v)
    done = archived_by_attacker(with_deltas(change_doc()))
    v = put_change(bearer, done, expected_version=v)
    landed = {**done, "state": "archived"}
    landed["apply"] = {**done["apply"], "landed_at": "2026-10-09T01:00:00Z"}
    assert put_change(bearer, landed, expected_version=v) == v + 1


def test_guard_rejects_unparseable_content_for_guarded_change(clients):
    bearer, _ = clients
    put_change(bearer, change_doc())
    for bad in (
        b"{not json",
        json.dumps([1, 2]).encode(),
        json.dumps({**change_doc(), "schema": 2}).encode(),
        json.dumps({**change_doc(), "name": "other"}).encode(),
        json.dumps({**change_doc(), "state": "done"}).encode(),
        json.dumps({**change_doc(), "meta": "x"}).encode(),
    ):
        _error(post_change(bearer, bad), 409, "change_invalid")
    assert stored_change(bearer) == change_doc()


def test_guard_cas_uses_version_it_checked(clients):
    bearer, _ = clients
    put_change(bearer, change_doc())
    body = _error(
        post_change(bearer, change_doc(), expected_version=5), 409, "version_conflict"
    )
    assert body["error"]["current"]["version"] == 1
    _error(
        post_change(bearer, change_doc(), expected_version=0), 409, "version_conflict"
    )


def test_guard_applies_to_ui_session_too(clients):
    """守衛不看認證方式：UI session 直接 blob_put 也不能繞過核准。"""
    bearer, ui = clients
    put_change(bearer, change_doc())
    _error(
        post_change(ui, archived_by_attacker(change_doc())),
        403,
        "authorization_required",
    )


# ── `POST /v1/tasks_authorization_status`（UI 判斷核准是否過期）──


def status(client, name: str = NAME):
    return client.post(
        "/v1/tasks_authorization_status", json={"vault": VAULT, "change": name}
    )


def test_authorization_status_follows_content_digest(clients):
    bearer, ui = clients
    _error(status(bearer), 403, "ui_session_required")
    assert status(ui).json() == {"change": None, "record": None, "approved": False}
    put_change(bearer, change_doc())
    data = status(ui).json()
    assert data["change"] == {
        "version": 1,
        "state": "active",
        "requires_authorization": True,
        "content_digest": tf.authorization_digest(change_doc()),
    }
    assert data["record"] is None and data["approved"] is False
    record = authorize(ui).json()["record"]
    data = status(ui).json()
    assert data["record"] == record and data["approved"] is True
    # archive 簿記寫入（版本前進、內容雜湊不變）：仍是已核准
    half = change_doc()
    half["meta"] = {**half["meta"], "notes": {"summary": "01N"}}
    put_change(bearer, half, expected_version=1)
    data = status(ui).json()
    assert data["change"]["version"] == 2 and data["approved"] is True
    # 內容修改：過期
    put_change(bearer, {**half, "proposal_md": "# 改過\n"}, expected_version=2)
    assert status(ui).json()["approved"] is False
    _error(
        ui.post(
            "/v1/tasks_authorization_status", json={"vault": VAULT, "change": "Bad"}
        ),
        400,
        "invalid_request",
    )


# ── 定義只有一份（api 層與任務層不漂移）──


def test_task_format_is_shared_with_task_layer():
    """服務端守衛與任務層用同一份定義：`remote_store` 直接取用 `api.task_format`。"""
    assert rs.authorization_digest is tf.authorization_digest
    assert rs.BOOKKEEPING_META_KEYS is tf.BOOKKEEPING_META_KEYS
    assert rs.SYNC_FIELDS is tf.SYNC_META_KEYS
    assert (rs.CHANGE_PREFIX, rs.AUTHORIZATION_PREFIX, rs.SCHEMA) == (
        tasks_admin.CHANGE_PREFIX,
        tasks_admin.AUTHORIZATION_PREFIX,
        tasks_admin.SCHEMA,
    )
    assert rs.STATES == tf.STATES and rs.PRINCIPAL_UI == tasks_admin.PRINCIPAL_UI
    # 段一／本機 archive 寫進 meta 的欄位都算簿記（雜湊不含它們）
    for key in (
        "vault",
        "notes",
        "note_digests",
        "note_id",
        "authorized_by",
        "authorization",
        "incomplete_at_archive",
        "archive_reason",
        "archived_at",
        rs.MIRROR_APPLYING_KEY,
        rs.MIRROR_APPLIED_KEY,
        "spec_applying",
        "spec_applied_caps",
        "spec_applied",
        *rs.SYNC_FIELDS,
    ):
        assert key in tf.BOOKKEEPING_META_KEYS, key
    base = change_doc()
    noisy = archived_by_attacker(base)
    assert tf.authorization_digest(noisy) == tf.authorization_digest(base)
    assert tf.authorization_digest(with_deltas(base)) != tf.authorization_digest(base)
    assert tf.authorization_digest(change_doc(requires=False)) != (
        tf.authorization_digest(base)
    )
