"""D14 雜項 vault（HTTP）：收料路由（客戶端免更新）、/pm init 後改進正式 vault、
保留 key、A17 歸屬與 scope 比對、注入快照排序。"""

from __future__ import annotations

import json

from .conftest import create_vault
from .test_spike_endpoints import episode, spike_concept


def _push(client, *episodes: dict) -> dict:
    resp = client.post("/v1/episodes", json={"episodes": list(episodes)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_unregistered_folder_episode_goes_to_misc(client):
    first = _push(client, episode(vault="folder/Desktop", repo="Desktop"))
    (result,) = first["results"]
    assert (result["status"], result["vault"], result["origin_key"]) == (
        "accepted",
        "misc",
        "folder/desktop",
    )
    assert first["created_vaults"] == ["misc"]
    second = _push(client, episode(session_id="s-2", vault="folder/other"))
    assert second["created_vaults"] == []
    assert second["results"][0]["vault"] == "misc"
    # 重送同一輪（客戶端仍送 folder key）→ duplicate
    again = _push(client, episode(vault="folder/Desktop", repo="Desktop"))
    assert again["results"][0]["status"] == "duplicate"
    items = client.get("/v1/episodes", params={"vault": "misc"}).json()["items"]
    assert len(items) == 2
    # 雜項不是 folder key 的解析目標：一般位置 vault_resolve 仍是 404
    resp = client.post("/v1/vault_resolve", json={"key": "folder/desktop"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_vault"


def test_pm_init_then_episodes_go_to_formal_vault(client):
    """`/pm init`＝vault_resolve(create=true)→POST /v1/vaults。

    之後同一 folder key 的新 episode 進正式 vault（不是雜項）。"""
    _push(client, episode(vault="folder/proj"))
    create_vault(client, "folder/proj")
    result = _push(client, episode(session_id="s-2", vault="folder/proj"))
    (item,) = result["results"]
    assert (item["status"], item["vault"]) == ("accepted", "folder/proj")
    assert "origin_key" not in item
    assert result["created_vaults"] == []


def test_remote_key_still_auto_created(client):
    result = _push(client, episode(vault="github.com/owner/new-repo"))
    assert result["created_vaults"] == ["github.com/owner/new-repo"]
    assert "origin_key" not in result["results"][0]


def test_client_cannot_send_misc(client):
    result = _push(client, episode(vault="misc"))
    assert result["results"][0]["status"] == "invalid"
    assert result["created_vaults"] == []


def test_misc_is_reserved_for_explicit_create_and_alias(client):
    for body in (
        {"key": "misc", "display": "m"},
        {"key": "folder/x", "display": "x", "kind": "misc"},
        {"key": "folder/x", "display": "x", "aliases": ["misc"]},
    ):
        resp = client.post("/v1/vaults", json=body)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == "reserved_vault"
    create_vault(client, "folder/p")
    resp = client.post(
        "/v1/vault_alias_add",
        json={"space": "dev", "vault": "folder/p", "alias": "misc"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "reserved_vault"


def test_concepts_from_misc_episodes_resolve_to_misc(client):
    """A17 (A)：來源 episode 在雜項 → concept 歸雜項；(B) scope 比對不會命中雜項。"""
    _push(client, episode(prompt_id="p-1", vault="folder/desk"))
    resp = client.post(
        "/v1/concepts",
        json={
            "vault": "*",
            "concepts": [
                spike_concept("c-1", scope="Desk", source_turns=[["p-1", 0]]),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert (result["vault"], result["resolved_by"]) == ("misc", "source_turns")
    for scope in ("misc", "雜項"):
        resp = client.post(
            "/v1/concepts",
            json={
                "vault": "*",
                "concepts": [
                    spike_concept("c-x", scope=scope, source_turns=[["gone", 0]])
                ],
            },
        )
        assert resp.status_code == 400, scope
        (rejected,) = resp.json()["error"]["results"]
        assert rejected["code"] == "vault_unresolved"


def test_injection_snapshot_lists_misc_concepts_last(client):
    create_vault(client, "github.com/owner/repo-x")
    _push(client, episode(prompt_id="p-9", session_id="s-9", vault="folder/desk"))
    resp = client.post(
        "/v1/concepts",
        json={
            "vault": "*",
            "concepts": [
                spike_concept("c-misc", scope="Desk", source_turns=[["p-9", 0]]),
                spike_concept(
                    "c-repo", vault="github.com/owner/repo-x", source_turns=[]
                ),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    exported = json.loads(client.get("/v1/concepts/export").content)
    assert [c["id"] for c in exported] == ["c-repo", "c-misc"]
    # 匯出格式不變（沒有多出 vault 等欄位）
    assert "vault" not in exported[0]
