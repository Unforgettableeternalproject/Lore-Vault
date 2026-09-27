"""A5 延伸到 HTTP 層：vault A 的請求拿不到 vault B 的任何東西。

沿用 `tests/storage/test_vault_filter.py` 的模式：
- `test_http_does_not_leak`：每個端點各做一次越界嘗試，全部不得洩漏
- `test_http_leak_test_is_load_bearing`：把儲存層的 vault 條件換成恆真，
  同一組嘗試必須抓到洩漏（證明測試本身會紅）
"""

from __future__ import annotations

import pytest

from lore_vault.storage import fts, notes, records, vectors

from .conftest import create_vault, embed_all, write_note

A = "folder/leak-a"
B = "folder/leak-b"
SHARED = "共同關鍵字 記憶系統 設計"


@pytest.fixture
def two_vaults(client, db_path):
    create_vault(client, A)
    create_vault(client, B)
    a = write_note(client, A, "A 的筆記", SHARED)
    b = write_note(client, B, "B 的秘密", SHARED)
    embed_all(db_path)
    return client, a["id"], b["id"]


def _leaks(client, a_id: str, b_id: str) -> list[str]:
    found: list[str] = []

    for mode in ("lexical", "vector", "hybrid"):
        data = client.post(
            "/v1/recall", json={"query": SHARED, "vault": A, "mode": mode}
        ).json()
        if any(i["id"] == b_id or i["vault"] != A for i in data["items"]):
            found.append(f"recall:{mode}")

    data = client.post("/v1/get", json={"vault": A, "ids": [a_id, b_id]}).json()
    if any(i["id"] == b_id for i in data["items"]) or b_id not in data["missing"]:
        found.append("get")

    data = client.post("/v1/list", json={"vault": A}).json()
    if any(i["id"] == b_id for i in data["items"]):
        found.append("list")

    data = client.post(
        "/v1/write", json={"vault": A, "title": "B 的秘密", "body": SHARED}
    ).json()
    if any(d["id"] == b_id for d in data["duplicates"]):
        found.append("write.duplicates")

    resp = client.post(
        "/v1/update",
        json={"vault": A, "id": b_id, "expected_updated": "x", "title": "竄改"},
    )
    if resp.status_code != 404:
        found.append("update")

    resolved = client.post("/v1/vault_resolve", json={"key": A}).json()
    status = client.post("/v1/status", json={"vault": A}).json()
    # A 只有自己的筆記（原本 1 篇 + 上面 write 的 1 篇）
    if resolved["note_count"] != 2 or status["vault"]["note_count"] != 2:
        found.append("note_count")
    return found


def test_http_does_not_leak(two_vaults):
    client, a_id, b_id = two_vaults
    assert _leaks(client, a_id, b_id) == []
    # B 沒有被 update 動到
    got = client.post("/v1/get", json={"vault": B, "ids": [b_id]}).json()
    assert got["items"][0]["title"] == "B 的秘密"


def test_wildcard_read_is_explicit(two_vaults):
    client, a_id, b_id = two_vaults
    data = client.post("/v1/recall", json={"query": SHARED, "vault": "*"}).json()
    assert {i["id"] for i in data["items"]} == {a_id, b_id}


def test_http_leak_test_is_load_bearing(two_vaults, monkeypatch):
    client, a_id, b_id = two_vaults

    def no_filter(scope, column):
        return "1 = 1", ()

    for module in (notes, fts, vectors, records):
        monkeypatch.setattr(module, "vault_clause", no_filter)
    assert set(_leaks(client, a_id, b_id)) == {
        "recall:lexical",
        "recall:vector",
        "recall:hybrid",
        "get",
        "list",
        "write.duplicates",
        "note_count",
    }
