"""A17：管線寫回 concept 時，未帶 vault 的 repo-scope 新 concept 的歸屬。

順序：既有 id（凍結）→ scope=None（global）→ 明確 vault（explicit）
→ (A) source_turns 的 episode vault → (B) scope 比對 vault 名稱 → 拒收。
A、B 都只解析到既有 vault，不猜、不自動建。
"""

from __future__ import annotations

import json

import pytest

from lore_vault.api import spike as spike_api

from .conftest import create_vault
from .test_spike_endpoints import episode, spike_concept

X = "github.com/owner/repo-x"
Y = "github.com/owner/repo-y"
Z = "github.com/owner/repo-z"


def _vaults(client, *keys: str) -> None:
    for key in keys or (X, Y):
        create_vault(client, key)


def _episode(client, prompt_id: str, vault: str, turn_index: int = 0) -> None:
    resp = client.post(
        "/v1/episodes",
        json={
            "episodes": [
                episode(
                    prompt_id=prompt_id,
                    turn_index=turn_index,
                    session_id=f"s-{prompt_id}",
                    vault=vault,
                )
            ]
        },
    )
    assert resp.json()["results"][0]["status"] == "accepted", resp.text


def _push(client, *concepts: dict, vault: str = "*"):
    return client.post(
        "/v1/concepts", json={"vault": vault, "concepts": list(concepts)}
    )


def _only(resp) -> dict:
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    return result


def _rejected(resp, status_code: int = 400) -> list[dict]:
    assert resp.status_code == status_code, resp.text
    error = resp.json()["error"]
    assert error["code"] == "batch_rejected"
    return error["results"]


def _exported_ids(client) -> list[str]:
    return [c["id"] for c in json.loads(client.get("/v1/concepts/export").content)]


# ── (A) source_turns ────────────────────────────────────────────────


def test_source_turns_decide_vault_before_scope(client):
    """episode 在 repo-y；scope=Repo-X 在 B 會唯一命中 repo-x——A 優先，拿掉 A 即紅。"""
    _vaults(client)
    _episode(client, "p-1", Y)
    result = _only(_push(client, spike_concept("c-1", source_turns=[["p-1", 0]])))
    assert result["vault"] == Y
    assert result["resolved_by"] == "source_turns"


def test_source_turns_partial_hit_uses_found_vault(client):
    _vaults(client)
    _episode(client, "p-1", Y)
    concept = spike_concept(
        "c-1", scope="Nowhere", source_turns=[["gone", 3], ["p-1", 0]]
    )
    result = _only(_push(client, concept))
    assert (result["vault"], result["resolved_by"]) == (Y, "source_turns")


def test_source_turns_match_turn_index_not_just_prompt(client):
    """同 prompt_id 不同 turn_index 不算命中（查不到 → 退 B）。"""
    _vaults(client)
    _episode(client, "p-1", Y, turn_index=1)
    result = _only(_push(client, spike_concept("c-1", source_turns=[["p-1", 0]])))
    assert (result["vault"], result["resolved_by"]) == (X, "scope_match")


def test_source_turns_ambiguous_rejects_without_falling_back(client):
    """來源輪次指向不同 vault → 歧義拒收；即使 B 會唯一命中也不退到 B。"""
    _vaults(client, X, Y, Z)
    _episode(client, "p-1", Y)
    _episode(client, "p-2", Z)
    concept = spike_concept("c-1", source_turns=[["p-1", 0], ["p-2", 0]])
    (result,) = _rejected(_push(client, concept))
    assert result["status"] == "invalid"
    assert result["code"] == spike_api.CODE_VAULT_AMBIGUOUS
    assert result["candidates"] == [Y, Z]
    assert "vault" not in result
    assert _exported_ids(client) == []


def test_no_episode_falls_back_to_scope(client):
    _vaults(client)
    result = _only(_push(client, spike_concept("c-1", source_turns=[["p-9", 0]])))
    assert (result["vault"], result["resolved_by"]) == (X, "scope_match")


# ── (B) scope 比對 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("vault", "scope"),
    [
        ({"key": X}, "Repo-X"),  # key 的 repo 段，不分大小寫
        ({"key": "folder/eternity-core", "display": "Eternity"}, "eternity"),
        (
            {"key": "github.com/o/new-name", "aliases": ["github.com/o/old-name"]},
            "Old-Name",
        ),
    ],
)
def test_scope_unique_match(client, vault, scope):
    create_vault(client, **vault)
    create_vault(client, Y)
    result = _only(_push(client, spike_concept("c-1", scope=scope)))
    assert result["vault"] == vault["key"]
    assert result["resolved_by"] == "scope_match"


def test_scope_ambiguous_suggests_org_repo(client):
    a, b = "github.com/org-a/tool", "github.com/org-b/tool"
    _vaults(client, a, b)
    (result,) = _rejected(_push(client, spike_concept("c-1", scope="Tool")))
    assert result["code"] == spike_api.CODE_VAULT_AMBIGUOUS
    assert result["candidates"] == [a, b]
    assert "org/repo" in result["error"]
    assert _exported_ids(client) == []


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("org-a/tool", "github.com/org-a/tool"), ("ORG-B/Tool", "github.com/org-b/tool")],
)
def test_scope_org_repo_disambiguates(client, scope, expected):
    _vaults(client, "github.com/org-a/tool", "github.com/org-b/tool")
    result = _only(_push(client, spike_concept("c-1", scope=scope)))
    assert (result["vault"], result["resolved_by"]) == (expected, "scope_match")


def test_scope_does_not_match_global_kind_vault(client):
    """kind=global 的 vault 不參與 B（scope 為 repo 名的 concept 不該掉進通用範圍）。"""
    create_vault(client, "shared/common", display="Common", kind="global")
    (result,) = _rejected(_push(client, spike_concept("c-1", scope="Common")))
    assert result["code"] == spike_api.CODE_VAULT_UNRESOLVED


# ── 全部失敗 ────────────────────────────────────────────────────────


def test_unresolved_rejects_whole_batch_and_creates_no_vault(client):
    _vaults(client)
    resp = _push(
        client,
        spike_concept("c-ok"),  # 單獨送會成功
        spike_concept("c-bad", scope="Nowhere", source_turns=[["p-9", 0]]),
    )
    ok, bad = _rejected(resp)
    assert ok["status"] == "created"  # 逐筆結果照列，但整批未寫
    assert bad["status"] == "invalid"
    assert bad["code"] == spike_api.CODE_VAULT_UNRESOLVED
    assert "vault" not in bad
    assert _exported_ids(client) == []
    missing = client.post("/v1/vault_resolve", json={"key": "nowhere"})
    assert missing.status_code == 404


# ── 其他路徑照舊 ────────────────────────────────────────────────────


def test_explicit_item_vault_wins(client):
    _vaults(client)
    _episode(client, "p-1", X)
    result = _only(_push(client, {**spike_concept("c-1"), "vault": Y}))
    assert (result["vault"], result["resolved_by"]) == (Y, "explicit")


def test_explicit_batch_vault_wins(client):
    _vaults(client)
    _episode(client, "p-1", X)
    result = _only(_push(client, spike_concept("c-1"), vault=Y))
    assert (result["vault"], result["resolved_by"]) == (Y, "explicit")


def test_scope_none_goes_global(client):
    _vaults(client)
    _episode(client, "p-1", X)
    result = _only(_push(client, spike_concept("c-1", scope=None)))
    assert (result["vault"], result["resolved_by"]) == ("global", "global")


def test_existing_id_stays_frozen(client):
    """已在 repo-y 的 id 再送（未帶 vault；A、B 都會指向 repo-x）→ 仍留在 repo-y。"""
    _vaults(client)
    _push(client, spike_concept("c-1"), vault=Y)
    _episode(client, "p-1", X)
    result = _only(_push(client, spike_concept("c-1", surprisal=0.5)))
    assert result == {
        "index": 0,
        "id": "c-1",
        "vault": Y,
        "resolved_by": "existing",
        "status": "updated",
    }


# ── 保護拿掉會紅 ────────────────────────────────────────────────────


def test_removing_source_turns_stage_changes_outcome(client, monkeypatch):
    """拿掉 A：`test_source_turns_decide_vault_before_scope` 的情境會落到 repo-x。"""
    monkeypatch.setattr(spike_api._VaultResolver, "by_source_turns", lambda *_: None)
    _vaults(client)
    _episode(client, "p-1", Y)
    result = _only(_push(client, spike_concept("c-1", source_turns=[["p-1", 0]])))
    assert result["vault"] == X != Y


def test_removing_scope_stage_changes_outcome(client, monkeypatch):
    """拿掉 B：`test_no_episode_falls_back_to_scope` 的情境會變成拒收。"""
    monkeypatch.setattr(spike_api._VaultResolver, "by_scope", lambda *_: None)
    _vaults(client)
    (result,) = _rejected(_push(client, spike_concept("c-1")))
    assert result["code"] == spike_api.CODE_VAULT_UNRESOLVED
