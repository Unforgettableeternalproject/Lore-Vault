"""UI 管理端點（T-70～T-75，`api.manage`）：成功／錯誤、兩段式確認、A20、
刪除後 doctor 維持綠。跨 space 洩漏另見 `test_manage_leak.py`。

新端點不在 conftest 的 `SPACED_PATHS`（不自動補 space），body 一律顯式帶 space。
"""

from __future__ import annotations

import json

import pytest

from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig
from lore_vault.documents.worker import DocumentWorker
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect
from lore_vault.storage.manage import MAX_MANUAL_RETRIES

from .conftest import DIM, FakeEmbedder, create_vault, write_note
from .test_spike_endpoints import episode, spike_concept

DEV = "folder/manage-a"
LORE = "lore/world"


def post(client, path: str, **body):
    return client.post(path, json=body)


def ok(resp, status: int = 200) -> dict:
    assert resp.status_code == status, resp.text
    return resp.json()


def code(resp) -> str:
    return resp.json()["error"]["code"]


def confirm(client, path: str, **body) -> dict:
    """兩段式：先規劃、再以 token 執行。回傳執行結果。"""
    planned = ok(post(client, path, **body))
    assert planned["executed"] is False and planned["confirm_token"]
    done = ok(post(client, path, **body, confirm_token=planned["confirm_token"]))
    assert done["executed"] is True
    return done


def doctor_fails(client) -> list[str]:
    report = ok(client.post("/v1/status"))["doctor"]
    names = {c["name"] for c in report["checks"]}
    assert {"tombstones.disjoint", "vaults.alias_integrity"} <= names
    return [c["name"] for c in report["checks"] if c["status"] == "fail"]


# ── T-70 vault 列表與編輯 ──


def test_vault_list_empty_space_returns_empty(client):
    data = ok(post(client, "/v1/vault_list", space="personal"))
    assert data == {"space": "personal", "vaults": []}


def test_vault_list_counts_and_updated(client):
    create_vault(client, DEV, aliases=["folder/old-a"])
    create_vault(client, "folder/manage-b")
    note = write_note(client, DEV, "標題", "內文")
    data = ok(post(client, "/v1/vault_list", space="dev"))
    by_key = {v["key"]: v for v in data["vaults"]}
    assert set(by_key) == {DEV, "folder/manage-b"}
    a = by_key[DEV]
    assert a["note_count"] == 1 and a["document_count"] == 0
    assert a["aliases"] == ["folder/old-a"] and a["origin"] == "manual"
    assert a["kind"] == "repo" and a["space"] == "dev"
    assert a["last_updated"] == note["updated"]
    assert by_key["folder/manage-b"]["last_updated"] is None


def test_vault_list_total_matches_vault_table(client, db_path):
    create_vault(client, DEV)
    client.post(
        "/v1/vaults", json={"key": LORE, "display": "世界", "space": "lore"}
    ).raise_for_status()
    total = sum(
        len(ok(post(client, "/v1/vault_list", space=s))["vaults"])
        for s in ("dev", "lore", "personal")
    )
    conn = connect(db_path)
    try:
        assert total == conn.execute("SELECT count(*) FROM vaults").fetchone()[0]
    finally:
        conn.close()


def test_vault_list_requires_space_and_rejects_unknown_fields(client):
    assert code(post(client, "/v1/vault_list")) == "space_required"
    assert code(post(client, "/v1/vault_list", space="nope")) == "invalid_space"
    assert post(client, "/v1/vault_list", space="dev", extra=1).status_code == 422


def test_vault_update_display(client):
    create_vault(client, DEV, aliases=["folder/old-a"])
    data = ok(
        post(
            client,
            "/v1/vault_update",
            space="dev",
            vault="folder/old-a",
            display="新名",
        )
    )
    assert data["key"] == DEV and data["display"] == "新名"
    assert code(
        post(client, "/v1/vault_update", space="dev", vault=DEV, display=" ")
    ) == ("invalid_request")
    resp = post(
        client, "/v1/vault_update", space="dev", vault="folder/none", display="x"
    )
    assert resp.status_code == 404 and code(resp) == "unknown_vault"


# ── T-71 別名 ──


def test_alias_add_then_resolve_then_remove(client):
    create_vault(client, DEV)
    data = ok(
        post(
            client,
            "/v1/vault_alias_add",
            space="dev",
            vault=DEV,
            alias="Folder/Renamed",
        )
    )
    assert data["aliases"] == ["folder/renamed"]
    resolved = ok(post(client, "/v1/vault_resolve", key="folder/renamed"))
    assert resolved["key"] == DEV and resolved["via_alias"] is True
    data = ok(
        post(
            client,
            "/v1/vault_alias_remove",
            space="dev",
            vault=DEV,
            alias="folder/renamed",
        )
    )
    assert data["aliases"] == []
    resp = post(client, "/v1/vault_resolve", key="folder/renamed")
    assert resp.status_code == 404


def test_alias_conflicts(client):
    create_vault(client, DEV, aliases=["folder/x-old"])
    create_vault(client, "folder/manage-b")
    # 已是別的 vault 的 key／別名 → 409 vault_exists（同 space 附 existing）
    for alias in ("folder/manage-b", "folder/x-old"):
        resp = post(
            client,
            "/v1/vault_alias_add",
            space="dev",
            vault="folder/manage-b",
            alias=alias,
        )
        assert resp.status_code == 409 and code(resp) == "vault_exists"
    resp = post(
        client, "/v1/vault_alias_add", space="dev", vault="folder/manage-b", alias=DEV
    )
    assert resp.json()["error"]["existing"] == {"key": DEV}
    # "*" 保留
    resp = post(client, "/v1/vault_alias_add", space="dev", vault=DEV, alias="*")
    assert code(resp) == "vault_required"


def test_alias_prefix_rule_in_non_dev(client):
    client.post(
        "/v1/vaults", json={"key": LORE, "display": "世界", "space": "lore"}
    ).raise_for_status()
    resp = post(client, "/v1/vault_alias_add", space="lore", vault=LORE, alias="world2")
    assert resp.status_code == 400 and code(resp) == "space_key_prefix_required"
    ok(post(client, "/v1/vault_alias_add", space="lore", vault=LORE, alias="lore/w2"))


def test_alias_remove_refuses_key_and_unknown_alias(client):
    create_vault(client, DEV, aliases=["folder/x-old"])
    create_vault(client, "folder/manage-b", aliases=["folder/b-old"])
    resp = post(client, "/v1/vault_alias_remove", space="dev", vault=DEV, alias=DEV)
    assert resp.status_code == 400 and code(resp) == "cannot_remove_key"
    # 別名屬於別的 vault → 404，不動它
    resp = post(
        client, "/v1/vault_alias_remove", space="dev", vault=DEV, alias="folder/b-old"
    )
    assert resp.status_code == 404 and code(resp) == "not_found"
    assert ok(post(client, "/v1/vault_resolve", key="folder/b-old"))["key"] == (
        "folder/manage-b"
    )


# ── T-72 換 space（A20）──


def _lore_vault(client, key: str = LORE, **extra) -> None:
    client.post(
        "/v1/vaults", json={"key": key, "display": key, "space": "lore", **extra}
    ).raise_for_status()


def test_move_space_two_phase(client):
    _lore_vault(client, aliases=["lore/old-world"])
    write_note(client, LORE, "設定", "世界觀", space="lore")
    body = {"space": "lore", "key": LORE, "to_space": "personal"}
    planned = ok(post(client, "/v1/vault_move_space", **body))
    assert planned["executed"] is False
    assert planned["plan"]["new_key"] == "personal/world"
    assert planned["plan"]["counts"]["notes.vault"] == 1
    # 規劃不改資料
    assert ok(post(client, "/v1/vault_resolve", key=LORE, space="lore"))["key"] == LORE
    done = ok(
        post(
            client,
            "/v1/vault_move_space",
            **body,
            confirm_token=planned["confirm_token"],
        )
    )
    assert done["executed"] is True
    assert done["vault"]["key"] == "personal/world"
    assert done["vault"]["aliases"] == ["personal/old-world"]
    assert done["vault"]["note_count"] == 1
    # 舊 key 不留別名、不再可解析
    for space in ("lore", "personal"):
        assert (
            post(client, "/v1/vault_resolve", key=LORE, space=space).status_code == 404
        )
    assert doctor_fails(client) == []


@pytest.mark.parametrize(
    ("space", "key", "to_space"),
    [
        ("dev", DEV, "lore"),
        ("dev", DEV, "personal"),
        ("lore", LORE, "dev"),
    ],
)
def test_move_space_refuses_dev_crossing(client, space, key, to_space):
    create_vault(client, DEV)
    _lore_vault(client)
    resp = post(client, "/v1/vault_move_space", space=space, key=key, to_space=to_space)
    assert resp.status_code == 400 and code(resp) == "space_change_refused"


def test_move_space_requires_formal_key_in_space(client):
    _lore_vault(client, aliases=["lore/alias"])
    for body in (
        {"space": "lore", "key": "lore/alias"},  # 別名不接受
        {"space": "personal", "key": LORE},  # 不在這個 space
    ):
        resp = post(client, "/v1/vault_move_space", **body, to_space="personal")
        assert resp.status_code == 404 and code(resp) == "unknown_vault"


def test_move_space_plan_changed_after_new_note(client):
    """規劃後 vault 內多了一則 note：引用筆數變了，確認時 409 plan_changed、不搬。"""
    _lore_vault(client)
    write_note(client, LORE, "一", "內容", space="lore")
    body = {"space": "lore", "key": LORE, "to_space": "personal"}
    planned = ok(post(client, "/v1/vault_move_space", **body))
    assert planned["plan"]["counts"]["notes.vault"] == 1
    write_note(client, LORE, "二", "規劃後新增", space="lore")
    resp = post(
        client, "/v1/vault_move_space", **body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 409 and code(resp) == "plan_changed"
    assert resp.json()["error"]["plan"]["counts"]["notes.vault"] == 2
    # 沒有搬：仍在 lore、兩則都在
    got = ok(post(client, "/v1/vault_resolve", key=LORE, space="lore"))
    assert got["note_count"] == 2
    assert (
        post(client, "/v1/vault_resolve", key="personal/world", space="personal")
    ).status_code == 404


def test_move_space_new_key_conflict(client):
    """目標 key（預設換前綴或指定 new_key）已被佔用：規劃階段就 409，不發 token。"""
    _lore_vault(client)
    client.post(
        "/v1/vaults",
        json={"key": "personal/world", "display": "占用", "space": "personal"},
    ).raise_for_status()
    client.post(
        "/v1/vaults",
        json={
            "key": "personal/other",
            "display": "別名占用",
            "space": "personal",
            "aliases": ["personal/taken"],
        },
    ).raise_for_status()
    for extra in ({}, {"new_key": "personal/taken"}):
        resp = post(
            client,
            "/v1/vault_move_space",
            space="lore",
            key=LORE,
            to_space="personal",
            **extra,
        )
        assert resp.status_code == 409 and code(resp) == "vault_conflict"
        assert "confirm_token" not in resp.text
    assert ok(post(client, "/v1/vault_resolve", key=LORE, space="lore"))["key"] == LORE


def test_move_space_same_space_is_refused(client):
    _lore_vault(client)
    resp = post(client, "/v1/vault_move_space", space="lore", key=LORE, to_space="lore")
    assert resp.status_code == 400 and code(resp) == "space_change_refused"


# ── 兩段式確認：竄改、過期、資料變動、誤用 ──


@pytest.fixture
def note_body(client):
    create_vault(client, DEV)
    note = write_note(client, DEV, "要刪", "內容")
    return {"space": "dev", "vault": DEV, "id": note["id"]}


def test_confirm_token_tampered(client, note_body):
    planned = ok(post(client, "/v1/note_delete", **note_body))
    token = planned["confirm_token"]
    encoded, sig = token.split(".")
    # 改 payload（換成別的 digest）但保留簽章
    import base64

    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["digest"] = "0" * 64
    forged = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        + "."
        + sig
    )
    for bad in (forged, token[:-2] + "xx", "garbage", token + ".x"):
        resp = post(client, "/v1/note_delete", **note_body, confirm_token=bad)
        assert resp.status_code == 400 and code(resp) == "invalid_confirm_token"
    # 沒有被刪
    assert ok(post(client, "/v1/list", vault=DEV))["items"]


def test_confirm_token_bound_to_args_and_op(client, note_body):
    other = write_note(client, DEV, "另一則", "內容")
    token = ok(post(client, "/v1/note_delete", **note_body))["confirm_token"]
    # 換目標、換 reason、換端點都不行
    for path, body in (
        ("/v1/note_delete", {**note_body, "id": other["id"]}),
        ("/v1/note_delete", {**note_body, "reason": "別的原因"}),
        ("/v1/document_delete", note_body),
    ):
        resp = post(client, path, **body, confirm_token=token)
        assert resp.status_code == 400 and code(resp) == "invalid_confirm_token"


def test_confirm_token_expired(client, note_body):
    planned = ok(post(client, "/v1/note_delete", **note_body))
    signer = client.app.state.lore_confirm
    real = signer.clock
    signer.clock = lambda: real() + signer.ttl + 1
    resp = post(
        client, "/v1/note_delete", **note_body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 400 and code(resp) == "confirm_token_expired"
    assert len(ok(post(client, "/v1/list", vault=DEV))["items"]) == 1


def test_confirm_refuses_when_data_changed_after_plan(client, db_path):
    create_vault(client, DEV)
    write_note(client, DEV, "一", "內容")
    body = {"space": "dev", "key": DEV}
    planned = ok(post(client, "/v1/vault_delete", **body))
    assert planned["plan"]["counts"]["notes"] == 1
    write_note(client, DEV, "二", "規劃後新增")
    resp = post(
        client, "/v1/vault_delete", **body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 409 and code(resp) == "plan_changed"
    assert resp.json()["error"]["plan"]["counts"]["notes"] == 2
    assert ok(post(client, "/v1/vault_resolve", key=DEV))["note_count"] == 2


def test_confirm_refuses_note_edited_after_plan(client, note_body):
    planned = ok(post(client, "/v1/note_delete", **note_body))
    current = ok(post(client, "/v1/get", vault=DEV, ids=[note_body["id"]]))["items"][0]
    ok(
        post(
            client,
            "/v1/update",
            vault=DEV,
            id=note_body["id"],
            expected_updated=current["updated"],
            body="改過了",
        )
    )
    resp = post(
        client, "/v1/note_delete", **note_body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 409 and code(resp) == "plan_changed"


def test_confirm_token_replay_does_not_repeat(client, note_body):
    planned = ok(post(client, "/v1/note_delete", **note_body))
    token = planned["confirm_token"]
    ok(post(client, "/v1/note_delete", **note_body, confirm_token=token))
    resp = post(client, "/v1/note_delete", **note_body, confirm_token=token)
    assert resp.status_code == 404


# ── T-73 刪除與墓碑 ──


def test_note_delete_tombstone_and_undelete(client):
    create_vault(client, DEV)
    note = write_note(client, DEV, "要刪", "內容")
    body = {"space": "dev", "vault": DEV, "id": note["id"], "reason": "測試"}
    done = confirm(client, "/v1/note_delete", **body)
    assert done["plan"]["note_ids"] == [note["id"]]
    assert ok(post(client, "/v1/list", vault=DEV))["items"] == []
    stones = ok(post(client, "/v1/tombstones", space="dev", vault=DEV))
    assert [(s["kind"], s["id"], s["reason"]) for s in stones["items"]] == [
        ("note", note["id"], "測試")
    ]
    assert stones["items"][0]["reimportable"] is False
    # v12 起墓碑有內容快照：可還原，列表帶標題供辨識
    assert stones["items"][0]["restorable"] is True
    assert stones["items"][0]["title"] == "要刪"
    assert doctor_fails(client) == []
    data = ok(post(client, "/v1/note_undelete", space="dev", id=note["id"]))
    assert data["restored"] is True and data["undeleted"]["note_id"] == note["id"]
    assert data["reimportable"] is False
    assert data["note"]["id"] == note["id"] and data["note"]["title"] == "要刪"
    assert ok(post(client, "/v1/tombstones", space="dev", vault=DEV))["items"] == []
    assert [i["id"] for i in ok(post(client, "/v1/list", vault=DEV))["items"]] == [
        note["id"]
    ]
    assert doctor_fails(client) == []
    resp = post(client, "/v1/note_undelete", space="dev", id=note["id"])
    assert resp.status_code == 404


def test_vault_delete_writes_tombstones_and_keeps_doctor_green(client):
    create_vault(client, DEV, aliases=["folder/a-old"])
    notes = [write_note(client, DEV, f"n{i}", "內容")["id"] for i in range(3)]
    done = confirm(client, "/v1/vault_delete", space="dev", key=DEV)
    assert done["plan"]["requires_force"] is True
    assert sorted(done["plan"]["note_ids"]) == sorted(notes)
    assert post(client, "/v1/vault_resolve", key=DEV).status_code == 404
    assert post(client, "/v1/vault_resolve", key="folder/a-old").status_code == 404
    # 已刪的 vault 仍可用原 key 查墓碑
    stones = ok(post(client, "/v1/tombstones", space="dev", vault=DEV))
    assert sorted(s["id"] for s in stones["items"]) == sorted(notes)
    assert all(s["vault_exists"] is False for s in stones["items"])
    assert doctor_fails(client) == []


def test_vault_delete_refuses_alias(client):
    create_vault(client, DEV, aliases=["folder/a-old"])
    resp = post(client, "/v1/vault_delete", space="dev", key="folder/a-old")
    assert resp.status_code == 404 and code(resp) == "unknown_vault"


def test_tombstones_pagination_and_errors(client):
    create_vault(client, DEV)
    ids = [write_note(client, DEV, f"n{i}", "內容")["id"] for i in range(5)]
    for note_id in ids:
        confirm(client, "/v1/note_delete", space="dev", vault=DEV, id=note_id)
    seen: list[str] = []
    cursor = None
    while True:
        page = ok(
            post(
                client, "/v1/tombstones", space="dev", vault="*", limit=2, cursor=cursor
            )
        )
        assert len(page["items"]) <= 2
        seen.extend(s["id"] for s in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))
    assert code(post(client, "/v1/tombstones", space="dev")) == "vault_required"
    assert code(
        post(client, "/v1/tombstones", space="dev", vault="*", cursor="!!")
    ) == ("invalid_cursor")
    resp = post(client, "/v1/tombstones", space="dev", vault="*", kinds=["concept"])
    assert code(resp) == "invalid_request"
    resp = post(client, "/v1/tombstones", space="dev", vault="folder/none")
    assert resp.status_code == 404 and code(resp) == "unknown_vault"


# ── 文件：刪除、復原、重試（T-73、T-74）──


def _config(blob_dir) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=200_000),
    )


@pytest.fixture
def blob_dir(tmp_path):
    return tmp_path / "blobs"


@pytest.fixture
def docs(make_client, blob_dir):
    client = make_client(config=_config(blob_dir), document_worker=False)
    create_vault(client, DEV)
    return client


@pytest.fixture
def run_worker(db_path, blob_dir):
    def run():
        conn = connect(db_path)
        try:
            DocumentWorker(
                conn,
                _config(blob_dir),
                blobs=BlobStore(blob_dir),
                embedder=FakeEmbedder(),
            ).run_once()
        finally:
            conn.close()

    return run


def _upload(client, name: str, data: bytes, vault: str = DEV, space: str = "dev"):
    resp = client.post(
        "/v1/documents",
        files={"file": (name, data, "text/plain")},
        data={"vault": vault, "space": space},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _doc(client, doc_id: str, vault: str = DEV, space: str = "dev") -> dict | None:
    items = ok(post(client, "/v1/list", vault=vault, space=space, kinds=["document"]))
    return next((i for i in items["items"] if i["id"] == doc_id), None)


def test_document_delete_undelete_roundtrip(docs, run_worker):
    up = _upload(docs, "notes.md", "# 標題\n\n世界觀設定內容".encode())
    run_worker()
    assert _doc(docs, up["document_id"])["status"] == "ready"
    body = {"space": "dev", "vault": DEV, "id": up["document_id"]}
    done = confirm(docs, "/v1/document_delete", **body)
    assert done["plan"]["document_id"] == up["document_id"]
    assert _doc(docs, up["document_id"]) is None
    stones = ok(
        post(docs, "/v1/tombstones", space="dev", vault=DEV, kinds=["document"])
    )
    assert stones["items"][0]["filename"] == "notes.md"
    assert stones["items"][0]["restorable"] is True
    assert doctor_fails(docs) == []

    data = ok(post(docs, "/v1/document_undelete", space="dev", id=up["document_id"]))
    assert data["document"]["id"] == up["document_id"]
    assert data["document"]["status"] == "pending"
    assert data["document"]["filename"] == "notes.md"
    run_worker()
    assert _doc(docs, up["document_id"])["status"] == "ready"
    got = ok(post(docs, "/v1/get", vault=DEV, ids=[up["document_id"]]))
    assert "世界觀" in got["items"][0]["text"]
    assert ok(post(docs, "/v1/tombstones", space="dev", vault=DEV))["items"] == []
    assert doctor_fails(docs) == []


def test_document_undelete_refusals(docs, run_worker, blob_dir, db_path):
    up = _upload(docs, "a.md", "# A\n\n內容一".encode())
    run_worker()
    confirm(docs, "/v1/document_delete", space="dev", vault=DEV, id=up["document_id"])
    # 同內容又上傳 → 復原會重複
    again = _upload(docs, "a.md", "# A\n\n內容一".encode())
    resp = post(docs, "/v1/document_undelete", space="dev", id=up["document_id"])
    assert resp.status_code == 409 and code(resp) == "not_restorable"
    assert resp.json()["error"]["reason"] == "duplicate"
    confirm(
        docs, "/v1/document_delete", space="dev", vault=DEV, id=again["document_id"]
    )
    # 原始檔不見 → blob_missing
    BlobStore(blob_dir).path_for(up["sha256"]).unlink()
    resp = post(docs, "/v1/document_undelete", space="dev", id=up["document_id"])
    assert resp.json()["error"]["reason"] == "blob_missing"
    # v11 前的舊墓碑（無檔名）→ incomplete
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE document_tombstones SET filename = NULL, mime = NULL "
            "WHERE document_id = ?",
            (again["document_id"],),
        )
    finally:
        conn.close()
    resp = post(docs, "/v1/document_undelete", space="dev", id=again["document_id"])
    assert resp.json()["error"]["reason"] == "incomplete"
    resp = post(docs, "/v1/document_undelete", space="dev", id="doc:none")
    assert resp.status_code == 404


def test_document_retry_failed_with_limit(docs, run_worker):
    up = _upload(docs, "bad.pdf", b"%PDF-1.4 not really a pdf")
    run_worker()
    assert _doc(docs, up["document_id"])["status"] == "failed"
    body = {"space": "dev", "vault": DEV, "id": up["document_id"]}
    for attempt in range(1, MAX_MANUAL_RETRIES + 1):
        data = ok(post(docs, "/v1/document_retry", **body))
        assert data["document"]["status"] == "pending"
        assert data["manual_retries"] == attempt
        # 已 pending：不可再排
        resp = post(docs, "/v1/document_retry", **body)
        assert resp.status_code == 409 and code(resp) == "not_failed"
        run_worker()
        assert _doc(docs, up["document_id"])["status"] == "failed"
    resp = post(docs, "/v1/document_retry", **body)
    assert resp.status_code == 409 and code(resp) == "retry_limit"
    resp = post(docs, "/v1/document_retry", space="dev", vault=DEV, id="doc:none")
    assert resp.status_code == 404


def test_document_endpoints_without_blob_dir(client):
    create_vault(client, DEV)
    resp = post(client, "/v1/document_undelete", space="dev", id="doc:x")
    assert resp.status_code == 500 and code(resp) == "documents_not_configured"


# ── T-75 concept 瀏覽與 episode 統計 ──

SECRET = "只存在對話原文裡的獨特字串-9f3a"
REPO = "github.com/owner/repo-x"


@pytest.fixture
def memory(client):
    create_vault(client, REPO, display="Repo-X")
    eps = [
        episode(
            prompt_id=f"p-{i}",
            machine=m,
            user_text=SECRET,
            assistant_text=SECRET,
        )
        for i, m in enumerate(["desk", "desk", "laptop"])
    ]
    ok(client.post("/v1/episodes", json={"episodes": eps}))

    def concept(i: int, **extra) -> dict:
        return spike_concept(
            f"c-{i}",
            probe=SECRET,
            why=SECRET,
            cue=SECRET,
            usability={"verdict": "APPLIED", "evidence": SECRET, "note": SECRET},
            **extra,
        )

    batches = [
        {"vault": REPO, "concepts": [concept(0), concept(1)]},
        # scope=None → 歸 global（自動建立）
        {"vault": "*", "concepts": [concept(2, scope=None)]},
    ]
    for batch in batches:
        ok(client.post("/v1/concepts", json={"mode": "upsert", **batch}))
    return client


def test_concept_query_filters_and_pages(memory):
    page = ok(post(memory, "/v1/concept_query", space="dev", vault="*", limit=2))
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = ok(
        post(
            memory,
            "/v1/concept_query",
            space="dev",
            vault="*",
            limit=2,
            cursor=page["next_cursor"],
        )
    )
    ids = [c["id"] for c in page["items"] + rest["items"]]
    assert sorted(ids) == ["c-0", "c-1", "c-2"] and rest["next_cursor"] is None
    item = page["items"][0]
    assert set(item) == {
        "id",
        "vault",
        "kind",
        "scope",
        "scope_state",
        "statement",
        "anchors",
        "surprisal",
        "usability_verdict",
        "updated",
    }
    repo_only = ok(
        post(memory, "/v1/concept_query", space="dev", vault=REPO, scope="repo-x")
    )
    assert sorted(c["id"] for c in repo_only["items"]) == ["c-0", "c-1"]
    glob = ok(
        post(memory, "/v1/concept_query", space="dev", vault="*", scope_state="global")
    )
    assert [c["id"] for c in glob["items"]] == ["c-2"]
    none = ok(post(memory, "/v1/concept_query", space="dev", vault="*", scope="nope"))
    assert none["items"] == [] and none["next_cursor"] is None
    resp = post(memory, "/v1/concept_query", space="dev", vault="*", scope_state="x")
    assert code(resp) == "invalid_request"


def test_episode_summary_counts_without_text(memory):
    data = ok(post(memory, "/v1/episode_summary", space="dev", vault="*"))
    assert data["total"] == 3
    assert [(m["machine"], m["episodes"]) for m in data["by_machine"]] == [
        ("desk", 2),
        ("laptop", 1),
    ]
    assert all(m["last_recorded"] for m in data["by_machine"])
    assert [(v["vault"], v["episodes"]) for v in data["by_vault"]] == [(REPO, 3)]


def test_memory_endpoints_never_return_conversation_text(memory):
    for path in ("/v1/concept_query", "/v1/episode_summary"):
        text = post(memory, path, space="dev", vault="*").text
        assert SECRET not in text
        assert "user_text" not in text and "assistant_text" not in text


def test_memory_endpoints_empty_outside_dev(memory):
    for space in ("lore", "personal"):
        assert (
            ok(post(memory, "/v1/concept_query", space=space, vault="*"))["items"] == []
        )
        assert (
            ok(post(memory, "/v1/episode_summary", space=space, vault="*"))["total"]
            == 0
        )
        resp = post(memory, "/v1/concept_query", space=space, vault=REPO)
        assert resp.status_code == 404 and code(resp) == "unknown_vault"
