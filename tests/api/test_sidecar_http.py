"""`POST /v1/blob_put`／`/v1/blob_get`（schema v17 側載，v18 加版本）：契約、錯誤碼、
space 範圍、版本鎖（`expected_version`／409 `version_conflict`）、依 key 前綴的上限、
不進任何檢索／快照路徑，以及 vault 刪除／換 space 後 doctor `sidecar.orphans` 維持綠。
任務層遠端同步開關（`tasks.remote_sync`）見 `test_runtime_settings.py`。

新端點不在 conftest 的 `SPACED_PATHS`，body 一律顯式帶 space。
"""

from __future__ import annotations

import base64
import sqlite3

from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import Config, EmbeddingConfig, UiConfig
from lore_vault.storage.sidecar import LARGE_MAX_BYTES, MAX_BYTES

from .conftest import (
    AUTH,
    DIM,
    UI_PASSWORD,
    UI_USER,
    create_vault,
    make_settings,
    seed_ui_account,
    write_note,
)

DEV = "folder/side-a"
DEV_B = "folder/side-b"
LORE = "lore/arc"
KEY = "tasks-snapshot"
MARKER = "zqxsidecarmarker"
PAYLOAD = f'{{"changes": ["{MARKER}"]}}'.encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def put(client, vault=DEV, key=KEY, data=PAYLOAD, space="dev", **extra):
    body = {"space": space, "key": key, "content_base64": b64(data), **extra}
    if vault is not None:
        body["vault"] = vault
    return client.post("/v1/blob_put", json=body)


def get(client, vault=DEV, key=KEY, space="dev"):
    body = {"space": space, "key": key}
    if vault is not None:
        body["vault"] = vault
    return client.post("/v1/blob_get", json=body)


def code(resp) -> str:
    return resp.json()["error"]["code"]


def test_put_then_get_roundtrip_and_overwrite(client):
    create_vault(client, DEV)
    resp = put(client, mime="application/json")
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"updated", "version"}
    assert resp.json()["version"] == 1
    got = get(client).json()
    assert got == {
        "vault": DEV,
        "key": KEY,
        "mime": "application/json",
        "content_base64": b64(PAYLOAD),
        "updated": resp.json()["updated"],
        "version": 1,
    }
    # 既有用法（不帶 expected_version）：照舊整份覆寫，版本遞增
    again = put(client, data=b"new")
    assert again.status_code == 200 and again.json()["version"] == 2
    got = get(client).json()
    assert base64.b64decode(got["content_base64"]) == b"new"
    assert got["mime"] == "application/octet-stream"
    assert got["version"] == 2


def test_errors(client):
    create_vault(client, DEV)
    create_vault(client, LORE, space="lore")
    assert put(client, data=b"x" * MAX_BYTES).status_code == 200
    resp = put(client, data=b"x" * (MAX_BYTES + 1))
    assert resp.status_code == 413 and code(resp) == "too_large"
    for bad in ("a/b", "a\\b", "..", ""):
        resp = put(client, key=bad)
        assert resp.status_code == 400 and code(resp) == "invalid_key", bad
        resp = get(client, key=bad)
        assert resp.status_code == 400 and code(resp) == "invalid_key", bad
    resp = client.post(
        "/v1/blob_put",
        json={"space": "dev", "vault": DEV, "key": KEY, "content_base64": "@@@"},
    )
    assert resp.status_code == 400 and code(resp) == "invalid_request"
    resp = put(client, mime="not a mime")
    assert resp.status_code == 400 and code(resp) == "invalid_request"
    # vault 在別的 space 與不存在相同
    for vault in ("folder/none", LORE):
        assert code(put(client, vault=vault)) == "unknown_vault"
        assert get(client, vault=vault).status_code == 404
    assert code(put(client, vault=None)) == "vault_required"
    resp = client.post(
        "/v1/blob_put", json={"vault": DEV, "key": KEY, "content_base64": b64(b"x")}
    )
    assert resp.status_code == 400 and code(resp) == "space_required"
    resp = put(client, extra_field=1)
    assert resp.status_code == 422


# ── 版本鎖（v18）──

TASK_KEY = "task-change:demo"


def test_expected_version_conflict_returns_current_content(client):
    create_vault(client, DEV)
    first = put(client, key=TASK_KEY, data=b"one", expected_version=0)
    assert first.status_code == 200 and first.json()["version"] == 1
    second = put(
        client, key=TASK_KEY, data=b"two", expected_version=1, mime="text/plain"
    )
    assert second.status_code == 200 and second.json()["version"] == 2
    stale = put(client, key=TASK_KEY, data=b"stale", expected_version=1)
    assert stale.status_code == 409 and code(stale) == "version_conflict"
    error = stale.json()["error"]
    # 形狀比照 note update：expected＋current；側載的 current 附完整內容與版本
    assert error["expected"] == 1
    assert error["current"] == {
        "vault": DEV,
        "key": TASK_KEY,
        "mime": "text/plain",
        "content_base64": b64(b"two"),
        "updated": second.json()["updated"],
        "version": 2,
    }
    # 被拒的寫入不改任何東西
    assert get(client, key=TASK_KEY).json() == error["current"]
    # 建立時預期不存在（0）但已存在：同樣衝突
    again = put(client, key=TASK_KEY, data=b"x", expected_version=0)
    assert again.status_code == 409 and again.json()["error"]["current"]["version"] == 2
    assert _doctor_status(client, "sidecar.version_conflict_integrity") == "pass"


def test_expected_version_on_missing_key_conflicts_with_null_current(client):
    create_vault(client, DEV)
    resp = put(client, key=TASK_KEY, expected_version=2)
    assert resp.status_code == 409 and code(resp) == "version_conflict"
    assert resp.json()["error"]["current"] is None
    assert code(get(client, key=TASK_KEY)) == "not_found"


def test_expected_version_validation(client):
    create_vault(client, DEV)
    for bad in (-1, "x", 1.5):
        assert put(client, expected_version=bad).status_code == 422, bad


def test_limit_depends_on_key_prefix(client):
    create_vault(client, DEV)
    big = b"x" * LARGE_MAX_BYTES
    assert put(client, key=TASK_KEY, data=big).status_code == 200
    resp = put(client, key=TASK_KEY, data=big + b"x")
    assert resp.status_code == 413 and code(resp) == "too_large"
    # `tasks-snapshot` 維持 64KB（不吃 task- 前綴的放寬）
    resp = put(client, key=KEY, data=b"x" * (MAX_BYTES + 1))
    assert resp.status_code == 413 and code(resp) == "too_large"


def test_get_missing_key_is_not_found(client):
    create_vault(client, DEV)
    resp = get(client)
    assert resp.status_code == 404 and code(resp) == "not_found"


def test_get_without_vault_lists_space(client):
    create_vault(client, DEV)
    create_vault(client, DEV_B)
    create_vault(client, LORE, space="lore")
    assert get(client, vault=None).json() == {"items": []}
    put(client, vault=DEV_B, data=b"b")
    put(client, vault=DEV, data=b"a")
    put(client, vault=DEV, key="other", data=b"o")
    put(client, vault=LORE, space="lore", data=b"secret-lore")
    items = get(client, vault=None).json()["items"]
    assert [(i["vault"], base64.b64decode(i["content_base64"])) for i in items] == [
        (DEV, b"a"),
        (DEV_B, b"b"),
    ]
    assert set(items[0]) == {
        "vault",
        "key",
        "mime",
        "content_base64",
        "updated",
        "version",
    }
    # `*` 與省略相同；別的 space 的內容不會出現
    assert get(client, vault="*").json()["items"] == items
    lore = get(client, vault=None, space="lore").json()["items"]
    assert [i["vault"] for i in lore] == [LORE]
    assert get(client, vault=None, space="personal").json() == {"items": []}


def test_sidecar_never_enters_search_list_or_snapshot(client, tmp_path):
    create_vault(client, DEV)
    write_note(client, DEV, "一般筆記", "內容與 zeppelin 有關")
    assert put(client).status_code == 200
    recall = client.post(
        "/v1/recall", json={"query": MARKER, "vault": "*", "kinds": ["note", "chunk"]}
    ).json()
    assert MARKER not in str(recall) and "tasks-snapshot" not in str(recall)
    listed = client.post("/v1/list", json={"vault": "*"}).json()
    assert [i["title"] for i in listed["items"]] == ["一般筆記"]
    assert MARKER not in str(listed)
    snap = client.get("/v1/snapshot")
    assert snap.status_code == 200
    path = tmp_path / "snap.db"
    path.write_bytes(snap.content)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM sidecar_blobs").fetchone()[0] == 0
    finally:
        conn.close()
    assert MARKER.encode() not in snap.content


def _doctor_status(client, name: str) -> str:
    checks = client.post("/v1/status").json()["doctor"]["checks"]
    return next(c["status"] for c in checks if c["name"] == name)


def _confirm(client, path: str, **body) -> dict:
    planned = client.post(path, json=body).json()
    assert planned["executed"] is False, planned
    done = client.post(path, json={**body, "confirm_token": planned["confirm_token"]})
    assert done.status_code == 200, done.text
    return {"planned": planned, "done": done.json()}


def test_vault_delete_removes_sidecar_and_doctor_stays_green(client):
    create_vault(client, DEV)
    put(client)
    result = _confirm(client, "/v1/vault_delete", space="dev", key=DEV)
    assert result["planned"]["plan"]["counts"]["sidecar_blobs"] == 1
    assert _doctor_status(client, "sidecar.orphans") == "pass"
    assert get(client, vault=None).json() == {"items": []}
    # 同 key 重建後不會讀到舊內容
    create_vault(client, DEV)
    assert code(get(client)) == "not_found"


def test_vault_move_space_moves_sidecar(client):
    create_vault(client, LORE, space="lore")
    put(client, vault=LORE, space="lore")
    result = _confirm(
        client, "/v1/vault_move_space", space="lore", key=LORE, to_space="personal"
    )
    counts = result["planned"]["plan"]["counts"]
    assert counts["sidecar_blobs.vault"] == 1 and counts["sidecar_blobs.space"] == 1
    assert get(client, vault=None, space="lore").json() == {"items": []}
    moved = get(client, vault="personal/arc", space="personal").json()
    assert base64.b64decode(moved["content_base64"]) == PAYLOAD
    assert _doctor_status(client, "sidecar.orphans") == "pass"


def test_requires_auth(db_path):
    with TestClient(create_app(make_settings(db_path))) as c:
        assert put(c).status_code == 401
        assert get(c).status_code == 401


def test_ui_session_can_read_and_write(db_path, tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    seed_ui_account(db_path)
    config = Config(
        embedding=EmbeddingConfig(dim=DIM), ui=UiConfig(static_dir=str(static))
    )
    ui = {"X-Lore-Vault-UI": "1"}
    app = create_app(make_settings(db_path, config=config))
    with TestClient(app, base_url="https://testserver") as c:
        resp = c.post(
            "/v1/vaults",
            json={"key": DEV, "display": DEV, "space": "dev"},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        login = c.post(
            "/ui/api/login",
            json={"username": UI_USER, "password": UI_PASSWORD},
            headers=ui,
        )
        assert login.status_code == 204, login.text
        body = {"space": "dev", "vault": DEV, "key": KEY}
        resp = c.post(
            "/v1/blob_put", json={**body, "content_base64": b64(b"u")}, headers=ui
        )
        assert resp.status_code == 200, resp.text
        resp = c.post("/v1/blob_get", json=body, headers=ui)
        assert resp.status_code == 200 and resp.json()["content_base64"] == b64(b"u")
        # cookie 有效但缺 CSRF header：403
        assert c.post("/v1/blob_get", json=body).status_code == 403
