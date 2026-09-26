"""A22 作者契約（HTTP）與可復原的 note 刪除。

- principal 只由憑證決定：body 帶 principal 一律 422，不寫入
- author 未填存 null、不代填；update 的 author 記為 updated_by（未填也是 null）
- 刪除 → 取消刪除：原 id、逐欄相同、FTS 可搜、doctor 維持綠
- v12 前的舊墓碑（無快照）維持只移除墓碑的舊行為
"""

from __future__ import annotations

import pytest

from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.storage.db import connect

from .conftest import create_vault, write_note

A = "folder/author-a"
NOTE_FIELDS = (
    "id",
    "vault",
    "title",
    "body",
    "summary",
    "topics",
    "links",
    "supersedes",
    "author",
    "principal",
    "updated_by",
    "updated_by_principal",
    "created",
    "updated",
)


def ok(resp, status: int = 200) -> dict:
    assert resp.status_code == status, resp.text
    return resp.json()


def code(resp) -> str:
    return resp.json()["error"]["code"]


def get_item(client, note_id: str, vault: str = A) -> dict:
    items = ok(client.post("/v1/get", json={"vault": vault, "ids": [note_id]}))["items"]
    assert len(items) == 1
    return items[0]


def note_count(db_path) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
    finally:
        conn.close()


def doctor_status(db_path, name: str) -> str:
    conn = connect(db_path)
    try:
        report = default_registry().run(
            DoctorContext(settings={"embedding_dim": 8}, resources={"db": conn})
        )
    finally:
        conn.close()
    return next(c for c in report.to_dict()["checks"] if c["name"] == name)["status"]


def doctor_fails(client) -> list[str]:
    report = ok(client.post("/v1/status"))["doctor"]
    names = {c["name"] for c in report["checks"]}
    assert {"notes.attribution", "tombstones.note_snapshots"} <= names
    return [c["name"] for c in report["checks"] if c["status"] == "fail"]


def delete_note(client, note_id: str, vault: str = A) -> None:
    body = {"space": "dev", "vault": vault, "id": note_id}
    planned = ok(client.post("/v1/note_delete", json=body))
    done = ok(
        client.post(
            "/v1/note_delete",
            json={**body, "confirm_token": planned["confirm_token"]},
        )
    )
    assert done["executed"] is True


# ── author／principal ──


def test_write_records_author_and_credential_principal(client):
    create_vault(client, A)
    written = write_note(client, A, "標題", "內容", author="Minka")
    assert (written["author"], written["principal"]) == ("Minka", "xavier")
    item = get_item(client, written["id"])
    assert item["author"] == "Minka" and item["principal"] == "xavier"
    # 建立時最後寫入者就是作者
    assert (item["updated_by"], item["updated_by_principal"]) == ("Minka", "xavier")
    listed = ok(client.post("/v1/list", json={"vault": A}))["items"][0]
    assert (listed["author"], listed["updated_by"]) == ("Minka", "Minka")
    recalled = ok(
        client.post("/v1/recall", json={"query": "標題", "vault": A, "mode": "lexical"})
    )["items"][0]
    assert recalled["author"] == "Minka" and "principal" not in recalled


def test_missing_author_is_not_filled_in(client):
    create_vault(client, A)
    written = write_note(client, A, "無名", "內容")
    assert written["author"] is None and written["principal"] == "xavier"
    item = get_item(client, written["id"])
    assert item["author"] is None and item["updated_by"] is None


def test_author_is_trimmed(client):
    create_vault(client, A)
    written = write_note(client, A, "標題", "內容", author="  Novia  ")
    assert written["author"] == "Novia"


@pytest.mark.parametrize("path", ["/v1/write", "/v1/update"])
def test_principal_in_body_is_rejected(client, db_path, path):
    """principal 只由憑證決定：body 帶了就 422，不寫入也不更新。"""
    create_vault(client, A)
    note = write_note(client, A, "標題", "內容")
    before = note_count(db_path)
    body = (
        {"vault": A, "title": "偽造", "body": "x"}
        if path == "/v1/write"
        else {"vault": A, "id": note["id"], "expected_updated": note["updated"]}
    )
    for field in ("principal", "updated_by_principal"):
        resp = client.post(path, json={**body, "body": "偽造", field: "mallory"})
        assert resp.status_code == 422, resp.text
        assert field in resp.text
    assert note_count(db_path) == before
    item = get_item(client, note["id"])
    assert item["body"] == "內容" and item["principal"] == "xavier"
    assert item["updated"] == note["updated"]


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("", "invalid_request"),
        ("   ", "invalid_request"),
        ("legacy", "invalid_request"),
        ("LEGACY", "invalid_request"),
        ("a" * 65, "invalid_request"),
        ("Min\nka", "invalid_request"),
        ("Min\x00ka", "invalid_characters"),
    ],
)
def test_invalid_author_is_rejected(client, db_path, author, expected):
    create_vault(client, A)
    resp = client.post(
        "/v1/write", json={"vault": A, "title": "t", "body": "b", "author": author}
    )
    assert resp.status_code == 400 and code(resp) == expected, resp.text
    assert note_count(db_path) == 0


def test_update_sets_updated_by_and_keeps_author(client):
    create_vault(client, A)
    note = write_note(client, A, "標題", "內容", author="Minka")
    first = ok(
        client.post(
            "/v1/update",
            json={
                "vault": A,
                "id": note["id"],
                "expected_updated": note["updated"],
                "body": "改過",
                "author": "Novia",
            },
        )
    )
    assert (first["author"], first["updated_by"]) == ("Minka", "Novia")
    assert first["updated_by_principal"] == "xavier"
    # 沒帶 author 的修改：updated_by 記為 null，不沿用上一位
    second = ok(
        client.post(
            "/v1/update",
            json={
                "vault": A,
                "id": note["id"],
                "expected_updated": first["updated"],
                "title": "新標題",
            },
        )
    )
    assert (second["author"], second["updated_by"]) == ("Minka", None)
    item = get_item(client, note["id"])
    assert (item["author"], item["updated_by"]) == ("Minka", None)


def test_doctor_attribution_goes_red_without_principal(client, db_path):
    create_vault(client, A)
    write_note(client, A, "標題", "內容")
    assert doctor_status(db_path, "notes.attribution") == "pass"
    conn = connect(db_path)
    try:
        conn.execute("UPDATE notes SET principal = NULL")
    finally:
        conn.close()
    assert doctor_status(db_path, "notes.attribution") == "fail"


# ── 可復原的刪除 ──


def test_delete_then_undelete_restores_every_field(client, db_path):
    create_vault(client, A)
    base = write_note(client, A, "舊篇", "被取代的內容")
    note = write_note(
        client,
        A,
        "可復原的筆記",
        "獨特關鍵詞 瑪那水晶 的內文",
        author="Minka",
        topics=["t1", "t2"],
        links=[base["id"]],
        supersedes=base["id"],
    )
    ok(
        client.post(
            "/v1/update",
            json={
                "vault": A,
                "id": note["id"],
                "expected_updated": note["updated"],
                "title": "可復原的筆記（改）",
                "author": "Novia",
            },
        )
    )
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE notes SET summary = '一句摘要' WHERE id = ?", (note["id"],)
        )
    finally:
        conn.close()
    before = get_item(client, note["id"])
    assert before["summary"] == "一句摘要"

    delete_note(client, note["id"])
    assert ok(client.post("/v1/get", json={"vault": A, "ids": [note["id"]]}))[
        "missing"
    ] == [note["id"]]
    assert doctor_fails(client) == []

    data = ok(client.post("/v1/note_undelete", json={"space": "dev", "id": note["id"]}))
    assert data["restored"] is True and data["reimportable"] is False
    assert data["note"]["author"] == "Minka"
    after = get_item(client, note["id"])
    assert {k: after[k] for k in NOTE_FIELDS} == {k: before[k] for k in NOTE_FIELDS}
    hits = ok(
        client.post(
            "/v1/recall", json={"query": "瑪那水晶", "vault": A, "mode": "lexical"}
        )
    )["items"]
    assert [h["id"] for h in hits] == [note["id"]]
    assert doctor_fails(client) == []


def test_old_tombstone_without_snapshot_keeps_old_behavior(client, db_path):
    create_vault(client, A)
    note = write_note(client, A, "舊墓碑", "內容")
    delete_note(client, note["id"])
    conn = connect(db_path)
    try:
        conn.execute("UPDATE note_tombstones SET snapshot = NULL")
    finally:
        conn.close()
    stones = ok(client.post("/v1/tombstones", json={"space": "dev", "vault": A}))
    assert stones["items"][0]["restorable"] is False
    assert stones["items"][0]["title"] is None
    data = ok(client.post("/v1/note_undelete", json={"space": "dev", "id": note["id"]}))
    assert data["restored"] is False and data["note"] is None
    assert data["reimportable"] is False
    assert ok(client.post("/v1/list", json={"vault": A}))["items"] == []
    assert (
        ok(client.post("/v1/tombstones", json={"space": "dev", "vault": A}))["items"]
        == []
    )


def test_undelete_refuses_when_vault_was_deleted(client):
    create_vault(client, A)
    note = write_note(client, A, "標題", "內容")
    planned = ok(client.post("/v1/vault_delete", json={"space": "dev", "key": A}))
    ok(
        client.post(
            "/v1/vault_delete",
            json={"space": "dev", "key": A, "confirm_token": planned["confirm_token"]},
        )
    )
    resp = client.post("/v1/note_undelete", json={"space": "dev", "id": note["id"]})
    assert resp.status_code == 409 and code(resp) == "not_restorable"
    assert resp.json()["error"]["reason"] == "vault_deleted"
    # 墓碑保留：重建 vault 後即可還原
    create_vault(client, A)
    data = ok(client.post("/v1/note_undelete", json={"space": "dev", "id": note["id"]}))
    assert data["restored"] is True
    assert get_item(client, note["id"])["title"] == "標題"
    assert doctor_fails(client) == []


def test_undelete_other_space_is_not_found(client):
    create_vault(client, A)
    note = write_note(client, A, "標題", "內容")
    delete_note(client, note["id"])
    resp = client.post("/v1/note_undelete", json={"space": "lore", "id": note["id"]})
    assert resp.status_code == 404


def test_doctor_snapshot_check_goes_red_on_bad_snapshot(client, db_path):
    create_vault(client, A)
    note = write_note(client, A, "標題", "內容")
    delete_note(client, note["id"])
    assert doctor_status(db_path, "tombstones.note_snapshots") == "pass"
    conn = connect(db_path)
    try:
        conn.execute('UPDATE note_tombstones SET snapshot = \'{"id": "other"}\'')
    finally:
        conn.close()
    assert doctor_status(db_path, "tombstones.note_snapshots") == "fail"
