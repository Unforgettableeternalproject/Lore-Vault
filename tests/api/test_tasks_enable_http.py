"""任務層啟用／停用／狀態端點與停用守衛（Vault 維護頁）。

- `/v1/tasks_enable`／`tasks_disable`／`tasks_status` 只收 UI session：bearer 一律 403
  `ui_session_required`（在 body 驗證之前）
- 啟用冪等：已存在的索引不覆寫（先放一份非空索引證明）；停用中則只移除 `disabled`
- 只開 dev space；`tasks.remote_sync` 關閉時 enable／disable 403（status 照常可讀）
- 停用中：任務內容與 `tasks-snapshot` 的 `blob_put` 403 `tasks_disabled`、
  `tasks_authorize` 403；內容位元組完全保留；重新啟用後原狀復原
- 索引本身：停用旗標只能單獨切換，整份重寫不能洗掉旗標
"""

from __future__ import annotations

import json

import pytest

from lore_vault.api import task_format as tf
from lore_vault.config import TasksConfig
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage.db import connect

from .test_tasks_authorize_http import (
    ALIAS,
    VAULT,
    _config,
    _error,
    b64,
    change_doc,
    open_client,  # noqa: F401 - fixture
    put_change,
)

OTHER = "folder/tasks-other"
TASK_KEYS = (
    "task-index",
    "task-change:guarded",
    "task-spec-mirror:demo",
    "task-decisions",
    "tasks-snapshot",
)


@pytest.fixture
def clients(open_client):  # noqa: F811
    bearer, ui = open_client(_config())
    for key, aliases in ((VAULT, [ALIAS]), (OTHER, [])):
        resp = bearer.post(
            "/v1/vaults", json={"key": key, "display": key, "aliases": aliases}
        )
        assert resp.status_code == 201, resp.text
    return bearer, ui


def enable(client, vault: str = VAULT, **body):
    return client.post("/v1/tasks_enable", json={"vault": vault, **body})


def disable(client, vault: str = VAULT, **body):
    return client.post("/v1/tasks_disable", json={"vault": vault, **body})


def status(client, **body):
    return client.post("/v1/tasks_status", json=body)


def put(bearer, key: str, data: dict | bytes, vault: str = VAULT, **extra):
    content = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    return bearer.post(
        "/v1/blob_put",
        json={
            "space": "dev",
            "vault": vault,
            "key": key,
            "mime": "application/json",
            "content_base64": b64(content),
            **extra,
        },
    )


def stored(db_path, key: str, vault: str = VAULT) -> storage_sidecar.SidecarBlob | None:
    conn = connect(db_path)
    try:
        try:
            return storage_sidecar.get(conn, vault, key, space="dev")
        except Exception:  # noqa: BLE001 - not_found
            return None
    finally:
        conn.close()


def index_doc(db_path) -> dict:
    blob = stored(db_path, tf.INDEX_KEY)
    assert blob is not None
    return json.loads(blob.content)


def ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 認證、space、開關 ────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["tasks_enable", "tasks_disable", "tasks_status"])
def test_bearer_is_rejected_before_body_validation(clients, db_path, path):
    bearer, _ = clients
    _error(
        bearer.post(f"/v1/{path}", json={"vault": VAULT}), 403, "ui_session_required"
    )
    _error(bearer.post(f"/v1/{path}", json={"x": 1}), 403, "ui_session_required")
    assert stored(db_path, tf.INDEX_KEY) is None


@pytest.mark.parametrize("space", ["lore", "personal"])
def test_non_dev_space_is_rejected(clients, db_path, space):
    _, ui = clients
    for resp in (
        enable(ui, space=space),
        disable(ui, space=space),
        status(ui, space=space),
    ):
        body = _error(resp, 400, "invalid_request")
        assert "dev" in body["error"]["message"]
    assert stored(db_path, tf.INDEX_KEY) is None


def test_remote_sync_off_rejects_enable_and_disable(open_client, db_path):  # noqa: F811
    bearer, ui = open_client(_config(tasks=TasksConfig(remote_sync=False)))
    assert bearer.post("/v1/vaults", json={"key": VAULT, "display": VAULT}).is_success
    _error(enable(ui), 403, "tasks_remote_sync_disabled")
    _error(disable(ui), 403, "tasks_remote_sync_disabled")
    assert stored(db_path, tf.INDEX_KEY) is None
    data = ok(status(ui))
    assert data["remote_sync"] is False
    assert data["vaults"][0]["enabled"] is False


# ── 啟用（冪等）與狀態 ────────────────────────────────────────────────


def test_enable_creates_empty_index_then_is_idempotent(clients, db_path):
    _, ui = clients
    first = ok(enable(ui, vault=ALIAS))
    assert first == {
        "vault": VAULT,
        "space": "dev",
        "created": True,
        "reenabled": False,
        "version": 1,
    }
    assert index_doc(db_path) == tf.empty_index()
    again = ok(enable(ui))
    assert again["created"] is False and again["reenabled"] is False
    assert again["version"] == 1


def test_enable_never_overwrites_existing_index(clients, db_path):
    """先以 bearer 放一份非空索引：啟用回 created false，版本與位元組都不變。"""
    bearer, ui = clients
    index = {"schema": 1, "changes": {"keep-me": {"state": "active"}}}
    assert put(bearer, tf.INDEX_KEY, index).status_code == 200
    before = stored(db_path, tf.INDEX_KEY)
    data = ok(enable(ui))
    assert data["created"] is False and data["reenabled"] is False
    after = stored(db_path, tf.INDEX_KEY)
    assert after.version == before.version and after.content == before.content


def test_status_reports_enabled_counts_and_states(clients):
    bearer, ui = clients
    ok(enable(ui))
    index = {
        "schema": 1,
        "changes": {
            "a": {"state": "active"},
            "b": {"state": "active"},
            "c": {"state": "pending_apply"},
            "d": {"state": "archived"},
        },
    }
    assert put(bearer, tf.INDEX_KEY, index, expected_version=1).status_code == 200
    data = ok(status(ui))
    assert data["space"] == "dev" and data["remote_sync"] is True
    rows = {r["vault"]: r for r in data["vaults"]}
    assert set(rows) == {VAULT, OTHER}
    assert rows[VAULT]["enabled"] is True and rows[VAULT]["initialized"] is True
    assert rows[VAULT]["changes"] == 4
    assert rows[VAULT]["states"] == {"active": 2, "pending_apply": 1, "archived": 1}
    assert rows[VAULT]["disabled"] is None
    assert rows[OTHER] == {
        "vault": OTHER,
        "initialized": False,
        "enabled": False,
        "disabled": None,
        "changes": None,
        "states": None,
        "version": 0,
    }
    single = ok(status(ui, vault=ALIAS))
    assert [r["vault"] for r in single["vaults"]] == [VAULT]


def test_status_flags_unparseable_index(clients):
    bearer, ui = clients
    assert put(bearer, tf.INDEX_KEY, b"not json").status_code == 200
    (row,) = ok(status(ui, vault=VAULT))["vaults"]
    assert row["initialized"] is True and row["enabled"] is False
    assert row["changes"] is None and "error" in row


# ── 停用（可逆、不刪內容）─────────────────────────────────────────────


def seed_task_content(bearer, ui) -> None:
    ok(enable(ui))
    put_change(bearer, change_doc())
    mirror = {"schema": 1, "capability": "demo", "exists": True, "text": "x"}
    for key, data in (
        ("task-spec-mirror:demo", mirror | {"source": "stdio"}),
        ("task-decisions", {"schema": 1, "decisions": {}, "source_digest": "0"}),
        ("tasks-snapshot", {"schema": 1, "changes": []}),
    ):
        assert put(bearer, key, data).status_code == 200


def snapshot_bytes(db_path) -> dict[str, tuple[int, bytes] | None]:
    result = {}
    for key in TASK_KEYS:
        blob = stored(db_path, key)
        result[key] = None if blob is None else (blob.version, blob.content)
    return result


def test_disable_blocks_task_writes_and_keeps_content(clients, db_path):
    bearer, ui = clients
    seed_task_content(bearer, ui)
    before_index = stored(db_path, tf.INDEX_KEY).content
    data = ok(disable(ui))
    assert data["changed"] is True
    assert data["disabled"]["by"] and data["disabled"]["at"].endswith("Z")
    frozen = snapshot_bytes(db_path)

    # 停用中：任務內容與快照一律拒收（不論 bearer 或 UI session）
    for client in (bearer, ui):
        _error(put(client, "task-change:guarded", change_doc()), 403, "tasks_disabled")
        _error(
            put(client, "task-change:new-one", change_doc("new-one")),
            403,
            "tasks_disabled",
        )
        for key in ("task-spec-mirror:demo", "task-decisions", "tasks-snapshot"):
            _error(put(client, key, {"schema": 1}), 403, "tasks_disabled")
    # 核准也拒絕
    resp = ui.post("/v1/tasks_authorize", json={"vault": VAULT, "change": "guarded"})
    _error(resp, 403, "tasks_disabled")
    # 索引整份重寫（舊版客戶端的 set_index_state）不能洗掉旗標
    rewrite = {"schema": 1, "changes": {"guarded": {"state": "archived"}}}
    _error(put(bearer, tf.INDEX_KEY, rewrite), 403, "tasks_disabled")
    # 讀取照常
    assert bearer.post(
        "/v1/blob_get",
        json={"space": "dev", "vault": VAULT, "key": "task-change:guarded"},
    ).is_success
    # 內容位元組完全保留（含索引：只多了 disabled）
    assert snapshot_bytes(db_path) == frozen

    # 冪等：不改時間、不寫入
    again = ok(disable(ui))
    assert again["changed"] is False and again["disabled"] == data["disabled"]
    assert snapshot_bytes(db_path) == frozen
    (row,) = ok(status(ui, vault=VAULT))["vaults"]
    assert row["enabled"] is False and row["disabled"] == data["disabled"]

    # 重新啟用：只移除 disabled，索引位元組回到停用前
    back = ok(enable(ui))
    assert back["created"] is False and back["reenabled"] is True
    assert stored(db_path, tf.INDEX_KEY).content == before_index
    restored = snapshot_bytes(db_path)
    for key in TASK_KEYS[1:]:
        assert restored[key] == frozen[key], key
    assert ok(enable(ui))["reenabled"] is False
    assert (
        put(bearer, "tasks-snapshot", {"schema": 1, "changes": []}).status_code == 200
    )


def test_disable_requires_existing_index(clients, db_path):
    _, ui = clients
    _error(disable(ui), 409, "tasks_not_enabled")
    assert stored(db_path, tf.INDEX_KEY) is None


def test_index_flag_can_only_be_toggled_alone(clients, db_path):
    """MCP／CLI 經 blob_put 停用與重新啟用：只切旗標可以；同時改 changes 不行。"""
    bearer, ui = clients
    ok(enable(ui))
    base = tf.empty_index()
    flag = {"at": "2026-10-09T00:00:00Z", "by": "agent"}
    # 沒有索引的 vault 不能直接寫成停用
    _error(
        put(bearer, tf.INDEX_KEY, base | {"disabled": flag}, vault=OTHER),
        403,
        "tasks_disabled",
    )
    # 格式不對 400
    _error(put(bearer, tf.INDEX_KEY, base | {"disabled": True}), 400, "invalid_request")
    # 停用同時改 changes 403
    mixed = {"schema": 1, "changes": {"x": {"state": "active"}}, "disabled": flag}
    _error(put(bearer, tf.INDEX_KEY, mixed), 403, "tasks_disabled")
    assert index_doc(db_path) == base
    # 單獨切換可以（兩個方向）
    assert put(bearer, tf.INDEX_KEY, base | {"disabled": flag}).status_code == 200
    assert index_doc(db_path)["disabled"] == flag
    _error(put(bearer, "tasks-snapshot", {"schema": 1}), 403, "tasks_disabled")
    assert put(bearer, tf.INDEX_KEY, base).status_code == 200
    assert index_doc(db_path) == base


def test_index_guard_uses_version_it_checked(clients):
    bearer, ui = clients
    ok(enable(ui))
    resp = put(bearer, tf.INDEX_KEY, tf.empty_index(), expected_version=7)
    _error(resp, 409, "version_conflict")


def test_disabled_flag_is_shared_definition():
    from lore_vault.tasks import remote_store as rs
    from lore_vault.tasks import snapshot

    assert rs.INDEX_KEY is tf.INDEX_KEY
    assert snapshot.SNAPSHOT_KEY is tf.SNAPSHOT_KEY
    assert tf.guarded_by_disable("task-change:x") and tf.guarded_by_disable(
        "tasks-snapshot"
    )
    assert not tf.guarded_by_disable(tf.INDEX_KEY)
