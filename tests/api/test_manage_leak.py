"""A5／A18 延伸到管理端點：以 space=lore 的請求碰不到 personal 的任何東西。

沿用 `test_vault_leak.py` 的模式：
- `test_manage_endpoints_do_not_leak`：每個新端點各做一次越界嘗試，全部不得洩漏
  （不回資料、不改資料、不在錯誤訊息帶出別 space 的 key）
- `test_manage_leak_test_is_load_bearing`：分別拿掉三道保護（vault 解析的 space
  條件、墓碑的 space 判定、刪除／換 space 的正式 key 檢查），同一組嘗試必須抓到洩漏
"""

from __future__ import annotations

import pytest

from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig
from lore_vault.storage.db import connect

from .conftest import DIM, create_vault, write_note

P = "personal/diary"
P_ALIAS = "personal/d-old"
LORE = "lore/world"
DEV = "folder/leak-dev"
DOC_ID = "doc:00000000-0000-0000-0000-00000000beef"


@pytest.fixture
def world(make_client, tmp_path, db_path):
    config = Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(tmp_path / "blobs")),
    )
    client = make_client(config=config, document_worker=False)
    create_vault(client, DEV)
    for key, space, aliases in ((LORE, "lore", []), (P, "personal", [P_ALIAS])):
        resp = client.post(
            "/v1/vaults",
            json={"key": key, "display": key, "space": space, "aliases": aliases},
        )
        assert resp.status_code == 201, resp.text
    live = write_note(client, P, "私人", "日記", space="personal")
    gone = write_note(client, P, "刪掉的", "日記", space="personal")
    planned = client.post(
        "/v1/note_delete", json={"space": "personal", "vault": P, "id": gone["id"]}
    ).json()
    done = client.post(
        "/v1/note_delete",
        json={
            "space": "personal",
            "vault": P,
            "id": gone["id"],
            "confirm_token": planned["confirm_token"],
        },
    )
    assert done.status_code == 200, done.text
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO document_tombstones (document_id, vault, sha256, deleted_at, "
            "reason, filename, mime, size_bytes, version) "
            "VALUES (?, ?, ?, '2026-09-01T00:00:00.000Z', 't', 'a.md', 'text/plain', "
            "1, 1)",
            (DOC_ID, P, "a" * 64),
        )
    finally:
        conn.close()
    return client, live["id"], gone["id"]


def _state(db_path) -> tuple:
    conn = connect(db_path)
    try:
        return (
            [
                tuple(r)
                for r in conn.execute(
                    "SELECT key, display, space FROM vaults ORDER BY key"
                )
            ],
            [
                tuple(r)
                for r in conn.execute(
                    "SELECT alias, vault FROM vault_aliases ORDER BY alias"
                )
            ],
            conn.execute("SELECT count(*) FROM notes").fetchone()[0],
            conn.execute("SELECT count(*) FROM note_tombstones").fetchone()[0],
            conn.execute("SELECT count(*) FROM document_tombstones").fetchone()[0],
        )
    finally:
        conn.close()


def _leaks(client, db_path, live_id: str, gone_id: str) -> list[str]:
    found: list[str] = []
    before = _state(db_path)

    def post(path: str, **body):
        return client.post(path, json={"space": "lore", **body})

    data = post("/v1/vault_list").json()
    if any(v["key"] == P for v in data["vaults"]):
        found.append("vault_list")

    attempts = {
        "vault_update": ("/v1/vault_update", {"vault": P, "display": "竄改"}),
        "vault_alias_add": (
            "/v1/vault_alias_add",
            {"vault": P, "alias": "lore/hijack"},
        ),
        "vault_alias_remove": (
            "/v1/vault_alias_remove",
            {"vault": P, "alias": P_ALIAS},
        ),
        "vault_move_space": (
            "/v1/vault_move_space",
            {"key": P, "to_space": "personal"},
        ),
        "vault_delete": ("/v1/vault_delete", {"key": P}),
        "note_delete": ("/v1/note_delete", {"vault": P, "id": live_id}),
        "document_delete": ("/v1/document_delete", {"vault": P, "id": DOC_ID}),
        "note_undelete": ("/v1/note_undelete", {"id": gone_id}),
        "document_undelete": ("/v1/document_undelete", {"id": DOC_ID}),
        "document_retry": ("/v1/document_retry", {"vault": P, "id": DOC_ID}),
        "tombstones:vault": ("/v1/tombstones", {"vault": P}),
        "concept_query": ("/v1/concept_query", {"vault": P}),
        "episode_summary": ("/v1/episode_summary", {"vault": P}),
    }
    for name, (path, body) in attempts.items():
        resp = post(path, **body)
        if resp.status_code != 404:
            found.append(f"{name}:{resp.status_code}")

    data = post("/v1/tombstones", vault="*").json()
    if any(s["vault"] == P for s in data.get("items", [])):
        found.append("tombstones:*")

    # 別名已被別 space 的 vault 佔用：409，但不帶出佔用者
    resp = client.post(
        "/v1/vault_alias_add", json={"space": "dev", "vault": DEV, "alias": P_ALIAS}
    )
    if resp.status_code != 409 or P in resp.text:
        found.append(f"alias_conflict:{resp.status_code}")

    if _state(db_path) != before:
        found.append("mutated")
    return found


def test_manage_endpoints_do_not_leak(world, db_path):
    client, live_id, gone_id = world
    assert _leaks(client, db_path, live_id, gone_id) == []


def _space_agnostic_lookup(conn, key, space):
    row = conn.execute("SELECT key FROM vaults WHERE key = ?", (key,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT vault FROM vault_aliases WHERE alias = ?", (key,)
        ).fetchone()
    if row is None:
        from lore_vault.storage.errors import UnknownVault

        raise UnknownVault(key)
    return row[0]


@pytest.mark.parametrize("guard", ["vault_lookup", "tombstone_space", "formal_key"])
def test_manage_leak_test_is_load_bearing(world, db_path, monkeypatch, guard):
    import lore_vault.api.manage as api_manage
    import lore_vault.storage.manage as storage_manage
    import lore_vault.storage.vaults as vaults

    if guard == "vault_lookup":
        monkeypatch.setattr(vaults, "_lookup", _space_agnostic_lookup)
    elif guard == "tombstone_space":
        monkeypatch.setattr(storage_manage, "_SPACE_OF", "'lore'")
        monkeypatch.setattr(storage_manage, "tombstone_space", lambda conn, key: "lore")
    else:
        from lore_vault.schema import canonical_key

        monkeypatch.setattr(
            api_manage,
            "_formal_key_in_space",
            lambda conn, key, space: canonical_key(key),
        )
    client, live_id, gone_id = world
    assert _leaks(client, db_path, live_id, gone_id) != []
