"""A18 延伸到 HTTP 層（T-54、T-55）：space 必填、無預設；跨 space 不洩漏。

- 每個讀寫端點漏帶 space → 400 `space_required`（不落回 dev）
- dev 與 lore 各一本、同關鍵字：space=dev 下 recall／get／list／write 查重／
  update／vault_resolve／status 都看不到 lore；`vault="*"` 只涵蓋 dev
- `test_http_space_leak_test_is_load_bearing`：拿掉 space 條件必須抓到洩漏
- hook／spike 端點不帶 space，固定 dev
"""

from __future__ import annotations

import pytest

from lore_vault.storage import fts, notes, records, vaults, vectors
from lore_vault.storage.errors import UnknownVault

from .conftest import OMIT, create_vault, embed_all, write_note
from .test_spike_endpoints import episode, spike_concept

DEV = "folder/space-dev"
LORE = "lore/space-arc"
SHARED = "共同關鍵字 世界觀 設定"


def _lore(client, key: str = LORE, **extra) -> dict:
    return create_vault(client, key, space="lore", **extra)


@pytest.fixture
def two_spaces(client, db_path):
    create_vault(client, DEV)
    _lore(client)
    d = write_note(client, DEV, "開發筆記", SHARED)
    lo = write_note(client, LORE, "世界觀祕密", SHARED, space="lore")
    embed_all(db_path)
    return client, d["id"], lo["id"]


REQUESTS = [
    ("/v1/vault_resolve", {"key": DEV}),
    ("/v1/vaults", {"key": "folder/new", "display": "n"}),
    ("/v1/recall", {"query": "設定", "vault": DEV}),
    ("/v1/get", {"vault": DEV, "ids": ["x"]}),
    ("/v1/list", {"vault": DEV}),
    ("/v1/write", {"vault": DEV, "title": "t", "body": "b"}),
    ("/v1/update", {"vault": DEV, "id": "x", "expected_updated": "x", "title": "t"}),
    ("/v1/status", {"vault": DEV}),
    ("/v1/status", {}),
]


@pytest.mark.parametrize(("path", "body"), REQUESTS)
def test_missing_space_is_400_not_dev(two_spaces, path, body):
    client, _, _ = two_spaces
    for value in (OMIT, None, ""):
        resp = client.post(path, json={**body, "space": value})
        assert resp.status_code == 400, (path, value, resp.text)
        assert resp.json()["error"]["code"] == "space_required"


@pytest.mark.parametrize(("path", "body"), REQUESTS)
def test_unknown_space_is_400(two_spaces, path, body):
    client, _, _ = two_spaces
    resp = client.post(path, json={**body, "space": "work"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_space"


def test_status_without_body_is_health_check(client):
    resp = client.post("/v1/status")
    assert resp.status_code == 200
    assert resp.json()["space"] is None


def _leaks(client, dev_id: str, lore_id: str) -> list[str]:
    found: list[str] = []
    for vault in (DEV, "*"):
        for mode in ("lexical", "vector", "hybrid"):
            data = client.post(
                "/v1/recall", json={"query": SHARED, "vault": vault, "mode": mode}
            ).json()
            if any(i["id"] == lore_id for i in data["items"]):
                found.append(f"recall:{vault}:{mode}")
        data = client.post("/v1/list", json={"vault": vault}).json()
        if any(i["id"] == lore_id for i in data["items"]):
            found.append(f"list:{vault}")
        data = client.post("/v1/get", json={"vault": vault, "ids": [lore_id]}).json()
        if data.get("items"):
            found.append(f"get:{vault}")

    data = client.post(
        "/v1/write", json={"vault": DEV, "title": "世界觀祕密", "body": SHARED}
    ).json()
    if any(d["id"] == lore_id for d in data["duplicates"]):
        found.append("write.duplicates")

    # 以 lore 的 key 配 space=dev：一律當不存在
    resp = client.post("/v1/vault_resolve", json={"key": LORE})
    if resp.status_code != 404:
        found.append("vault_resolve")
    resp = client.post("/v1/list", json={"vault": LORE})
    if resp.status_code != 404:
        found.append("list:lore-key")
    resp = client.post(
        "/v1/update",
        json={"vault": LORE, "id": lore_id, "expected_updated": "x", "title": "竄改"},
    )
    if resp.status_code != 404:
        found.append("update")
    resp = client.post("/v1/status", json={"vault": LORE})
    if resp.status_code != 404:
        found.append("status")
    return found


def test_http_does_not_leak_across_spaces(two_spaces):
    client, dev_id, lore_id = two_spaces
    assert _leaks(client, dev_id, lore_id) == []
    # lore 本身完好，且 lore 下看不到 dev
    got = client.post(
        "/v1/get", json={"vault": LORE, "ids": [lore_id, dev_id], "space": "lore"}
    ).json()
    assert [i["title"] for i in got["items"]] == ["世界觀祕密"]
    assert got["missing"] == [dev_id]
    data = client.post("/v1/recall", json={"query": SHARED, "vault": "*"}).json()
    assert {i["vault"] for i in data["items"]} == {DEV}


def test_http_space_leak_test_is_load_bearing(two_spaces, monkeypatch):
    client, dev_id, lore_id = two_spaces

    def no_space(scope, column):
        if scope.is_all:
            return "1 = 1", ()
        return f"{column} = ?", (scope.key,)

    for module in (notes, fts, vectors, records):
        monkeypatch.setattr(module, "vault_clause", no_space)
    original = vaults._lookup

    def any_space(conn, key, space):
        for candidate in ("dev", "lore", "personal"):
            try:
                return original(conn, key, candidate)
            except UnknownVault:
                continue
        raise UnknownVault(key)

    monkeypatch.setattr(vaults, "_lookup", any_space)
    found = set(_leaks(client, dev_id, lore_id))
    assert {
        "recall:*:lexical",
        "list:*",
        "get:*",
        "vault_resolve",
        "list:lore-key",
        "update",
        "status",
    } <= found


# ── 非 dev vault 的建立 ──


def test_non_dev_key_requires_prefix(client):
    resp = client.post(
        "/v1/vaults", json={"key": "aeswir-arc", "display": "arc", "space": "lore"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "space_key_prefix_required"
    created = _lore(client, "lore/aeswir-arc")
    assert created["space"] == "lore"
    assert created["key"] == "lore/aeswir-arc"
    # lore 不自動建 global
    resp = client.post(
        "/v1/vault_resolve", json={"key": "lore/global", "space": "lore"}
    )
    assert resp.status_code == 404


def test_same_key_in_other_space_conflicts(client):
    """key 全域唯一：已在 lore 的 key 不能在 dev 再建一次。"""
    _lore(client)
    resp = client.post("/v1/vaults", json={"key": LORE, "display": "x", "space": "dev"})
    assert resp.status_code == 409


# ── hook／spike 端點：不帶 space，固定 dev ──


def test_spike_endpoints_need_no_space_and_stay_dev(client, db_path):
    _lore(client)
    resp = client.post(
        "/v1/episodes",
        json={"episodes": [episode(), episode(prompt_id="p-2", vault=LORE)]},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["status"] == "accepted"
    # 已在 lore 的 key 不會被 episode 收料當成 dev vault
    assert results[1]["status"] == "invalid"

    resp = client.post(
        "/v1/concepts",
        json={"vault": LORE, "concepts": [spike_concept("c-1")]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_vault"

    resp = client.get("/v1/episodes", params={"vault": "*"})
    assert resp.status_code == 200
    assert [e["vault"] for e in resp.json()["items"]] == ["github.com/owner/repo-x"]
