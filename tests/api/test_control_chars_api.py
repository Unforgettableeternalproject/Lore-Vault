"""HTTP 層的控制字元防護。

write／update 拒收（invalid_characters）；episode 收料清理並標記。
"""

from __future__ import annotations

import json

import pytest

from lore_vault.storage.db import connect

from .conftest import create_vault, write_note
from .test_spike_endpoints import episode

VAULT = "folder/cc"


def _post(client, path: str, payload: dict):
    """以 ASCII 跳脫送 JSON。

    孤立 surrogate 無法編成 UTF-8，httpx 的 json= 會先炸在客戶端。"""
    return client.post(
        path,
        content=json.dumps(payload, ensure_ascii=True),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    ("extra", "field", "index"),
    [
        ({"title": "標題\u0000"}, "title", 2),
        ({"body": "正文\u0007尾"}, "body", 2),
        ({"body": "x\ud800"}, "body", 1),
        ({"topics": ["ok", "\u0000t"]}, "topics[1]", 0),
        ({"links": ["a\u001b"]}, "links[0]", 1),
        ({"supersedes": "n\u0000"}, "supersedes", 1),
    ],
)
def test_write_rejects_forbidden_characters(client, extra, field, index):
    create_vault(client, VAULT)
    payload = {"vault": VAULT, "title": "標題", "body": "正文", **extra}
    resp = _post(client, "/v1/write", payload)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_characters"
    assert (error["field"], error["index"]) == (field, index)
    # 不回顯內容
    assert "正文" not in resp.text and "標題" not in error["message"]
    listed = client.post("/v1/list", json={"vault": VAULT}).json()
    assert listed["items"] == []


def test_write_keeps_tab_newline_carriage_return(client):
    create_vault(client, VAULT)
    note = write_note(client, VAULT, "t", "a\tb\r\nc")
    got = client.post("/v1/get", json={"vault": VAULT, "ids": [note["id"]]}).json()
    assert got["items"][0]["body"] == "a\tb\r\nc"


@pytest.mark.parametrize(
    "extra",
    [
        {"title": "t\u0000"},
        {"body": "b\u0000"},
        {"topics": ["\u0001"]},
        {"links": ["\udfff"]},
        {"supersedes": "\u0000"},
    ],
)
def test_update_rejects_forbidden_characters(client, extra):
    create_vault(client, VAULT)
    note = write_note(client, VAULT, "t", "b")
    resp = _post(
        client,
        "/v1/update",
        {
            "vault": VAULT,
            "id": note["id"],
            "expected_updated": note["updated"],
            **extra,
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_characters"
    got = client.post("/v1/get", json={"vault": VAULT, "ids": [note["id"]]}).json()
    assert got["items"][0]["updated"] == note["updated"]


def test_episode_with_nul_is_sanitized_and_marked(client, db_path):
    """舊客戶端沒清理就送：服務端清理後收下並標 sanitized；新客戶端送清理後的同一輪
    得到 duplicate（清理冪等）。"""
    raw = episode(user_text="問\u0000題", assistant_text="答\u0001")
    first = client.post("/v1/episodes", json={"episodes": [raw]}).json()
    item = first["results"][0]
    assert item["status"] == "accepted"
    assert item["sanitized"] is True and item["sanitized_chars"] == 2

    cleaned = episode(user_text="問\\0題", assistant_text="答\\x01")
    second = client.post("/v1/episodes", json={"episodes": [cleaned]}).json()
    assert second["results"][0]["status"] == "duplicate"
    assert "sanitized" not in second["results"][0]
    again = client.post("/v1/episodes", json={"episodes": [raw]}).json()
    assert again["results"][0]["status"] == "duplicate"

    conn = connect(db_path)
    try:
        data = json.loads(conn.execute("SELECT data FROM episodes").fetchone()[0])
    finally:
        conn.close()
    assert data["user_text"] == "問\\0題" and data["assistant_text"] == "答\\x01"
    status = client.post("/v1/status", json={}).json()
    checks = {c["name"]: c["status"] for c in status["doctor"]["checks"]}
    assert checks["storage.control_chars"] == "pass"
