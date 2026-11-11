"""U.E.P 寫入審核契約（add-uep-interface）的服務端。

- uep 專屬 token → principal `uep`；未設定時系統行為不變；兩組 token 相同拒絕啟動
- uep 只能用 dev／lore（personal 與其他 space 403）、不能 undelete／上傳文件
- lore：write／update → pending；delete → pending_delete（note 照常可見）
- 審核端點只收 UI session：approve／reject；狀態不符 409
- recall／get／list 帶 `review: unverified`；rejected 不進 recall、ask，get 看得到
- doctor `notes.review` 每一條規則都有「破壞後轉紅」的測試
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import auth as auth_mod
from lore_vault.api import manage as manage_api
from lore_vault.api import routes
from lore_vault.api.app import create_app
from lore_vault.ask import service as ask_service
from lore_vault.config import ConfigError, Secret
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.notes import review as review_service
from lore_vault.notes import service as notes_service_mod
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.db import connect
from lore_vault.storage.migrate import MIGRATIONS, SCHEMA_VERSION, migrate

from .conftest import (
    AUTH,
    UI_LOGIN,
    SpaceClient,
    create_vault,
    make_settings,
    seed_ui_account,
    write_note,
)

UI = {"X-Lore-Vault-UI": "1"}
UEP_TOKEN = "uep-token-0123456789abcdef"
UEP_AUTH = {"Authorization": f"Bearer {UEP_TOKEN}"}
LORE = "lore/arc"
PERSONAL = "personal/diary"
DEV = "folder/uep-dev"
KEYWORD = "月蝕 結界 設定"


class Rig:
    def __init__(self, owner: TestClient, uep: TestClient, ui: TestClient, db_path):
        self.owner, self.uep, self.ui, self.db_path = owner, uep, ui, db_path

    def uep_write(self, title="UEP 筆記", body=KEYWORD, space="lore", vault=LORE):
        resp = self.uep.post(
            "/v1/write",
            json={"space": space, "vault": vault, "title": title, "body": body},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def get(self, client, note_id, space="lore", vault=LORE):
        resp = client.post(
            "/v1/get", json={"space": space, "vault": vault, "ids": [note_id]}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["items"][0]

    def review(self, action, note_id, expected_updated=None, **extra):
        if expected_updated is None:  # 預設審核「現在看到的這一版」
            expected_updated = self.get(self.owner, note_id)["updated"]
        return self.ui.post(
            f"/v1/review/{action}",
            json={
                "space": "lore",
                "vault": LORE,
                "id": note_id,
                "expected_updated": expected_updated,
                **extra,
            },
        )

    def delete(self, client, note_id, space="lore", vault=LORE, **extra):
        body = {"space": space, "vault": vault, "id": note_id, **extra}
        plan = client.post("/v1/note_delete", json=body)
        assert plan.status_code == 200, plan.text
        assert plan.json()["executed"] is False
        return client.post(
            "/v1/note_delete",
            json={**body, "confirm_token": plan.json()["confirm_token"]},
        )

    def recall(self, client=None, space="lore", vault=LORE):
        resp = (client or self.owner).post(
            "/v1/recall", json={"space": space, "vault": vault, "query": KEYWORD}
        )
        assert resp.status_code == 200, resp.text
        return {i["id"]: i for i in resp.json()["items"]}


@pytest.fixture
def rig(db_path):
    seed_ui_account(db_path)
    settings = make_settings(db_path, uep_token=Secret(UEP_TOKEN))
    app = create_app(settings)
    opened = []
    owner = SpaceClient(app)
    owner.__enter__()
    owner.headers.update(AUTH)
    uep = SpaceClient(app)
    uep.__enter__()
    uep.headers.update(UEP_AUTH)
    ui = TestClient(app, base_url="https://testserver")
    ui.__enter__()
    assert ui.post("/ui/api/login", json=UI_LOGIN, headers=UI).status_code == 204
    ui.headers.update(UI)
    opened.extend([ui, uep, owner])
    create_vault(owner, LORE, space="lore")
    create_vault(owner, PERSONAL, space="personal")
    create_vault(owner, DEV)
    yield Rig(owner, uep, ui, db_path)
    for c in opened:
        c.__exit__(None, None, None)


def _error(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


# ── 端到端 ──


def test_uep_write_recall_unverified_then_ui_approve(rig):
    created = rig.uep_write()
    assert created["review_state"] == "pending"
    assert created["principal"] == "uep"
    item = rig.recall()[created["id"]]
    assert item["review"] == "unverified"
    got = rig.get(rig.owner, created["id"])
    assert (got["review_state"], got["review"]) == ("pending", "unverified")
    assert got["reviewed_by"] is None

    resp = rig.review("approve", created["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review_state"] == "verified"
    assert body["reviewed_by"] == "UEPBernie"
    assert body["deleted"] is False
    assert "review" not in rig.recall()[created["id"]]
    got = rig.get(rig.uep, created["id"])
    assert got["review_state"] == "verified" and "review" not in got
    assert got["reviewed_at"]
    # 審核動作不改 updated（樂觀鎖版本不變）
    assert got["updated"] == created["updated"]


def test_uep_update_of_verified_note_goes_back_to_pending(rig):
    created = rig.uep_write()
    rig.review("approve", created["id"])
    resp = rig.uep.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": created["id"],
            "expected_updated": created["updated"],
            "body": KEYWORD + " 修訂",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] == "pending"
    got = rig.get(rig.owner, created["id"])
    assert got["review_state"] == "pending"
    assert (got["reviewed_by"], got["reviewed_at"], got["review_reason"]) == (
        None,
        None,
        None,
    )


def test_uep_cannot_update_note_it_did_not_write(rig):
    foreign = write_note(rig.owner, LORE, "他人筆記", KEYWORD, space="lore")
    resp = rig.uep.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": foreign["id"],
            "expected_updated": foreign["updated"],
            "title": "改掉",
        },
    )
    _error(resp, 403, "uep_not_author")
    assert rig.get(rig.owner, foreign["id"])["title"] == "他人筆記"


def test_uep_delete_is_pending_delete_then_approve_tombstones(rig):
    created = rig.uep_write()
    rig.review("approve", created["id"])
    resp = rig.delete(rig.uep, created["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["executed"] is True
    assert resp.json()["review_state"] == "pending_delete"
    got = rig.get(rig.owner, created["id"])  # note 照常可見
    assert (got["review_state"], got["review"]) == ("pending_delete", "unverified")
    assert created["id"] in rig.recall()
    # 重複發刪除請求
    again = rig.uep.post(
        "/v1/note_delete",
        json={"space": "lore", "vault": LORE, "id": created["id"]},
    )
    _error(again, 409, "review_state_conflict")

    done = rig.review("approve", created["id"])
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] is True
    missing = rig.owner.post(
        "/v1/get", json={"space": "lore", "vault": LORE, "ids": [created["id"]]}
    )
    assert missing.json()["missing"] == [created["id"]]
    tombs = rig.owner.post(
        "/v1/tombstones", json={"space": "lore", "vault": LORE, "limit": 10}
    ).json()["items"]
    assert [t["id"] for t in tombs] == [created["id"]]

    # 使用者還原：回到刪除請求之前的狀態（verified），不再是待刪
    back = rig.ui.post("/v1/note_undelete", json={"space": "lore", "id": created["id"]})
    assert back.status_code == 200, back.text
    restored = rig.get(rig.owner, created["id"])
    assert restored["review_state"] == "verified"


def test_uep_can_request_delete_of_other_principals_note(rig):
    foreign = write_note(rig.owner, LORE, "他人筆記", KEYWORD, space="lore")
    resp = rig.delete(rig.uep, foreign["id"])
    assert resp.json()["review_state"] == "pending_delete"
    # 駁回刪除：還原成 none（原本不在審核流程中）
    rej = rig.review("reject", foreign["id"], reason="還要用")
    assert rej.status_code == 200, rej.text
    got = rig.get(rig.owner, foreign["id"])
    assert got["review_state"] == "none"
    assert got["review_reason"] == "還要用"


def test_reject_pending_hides_from_recall_and_ask_but_get_shows(rig, db_path):
    created = rig.uep_write()
    assert created["id"] in rig.recall()
    rej = rig.review("reject", created["id"], reason="內容不實")
    assert rej.status_code == 200, rej.text
    assert rej.json()["review_state"] == "rejected"
    assert created["id"] not in rig.recall()
    got = rig.get(rig.owner, created["id"])
    assert (got["review_state"], got["review_reason"]) == ("rejected", "內容不實")
    conn = connect(db_path)
    try:
        ctx = ask_service.prepare(
            conn, KEYWORD, LORE, space="lore", embedder=None, dim=None
        )
    finally:
        conn.close()
    assert created["id"] not in [s.id for s in ctx.snippets]


def test_ask_marks_unverified_snippets(rig, db_path):
    created = rig.uep_write()
    conn = connect(db_path)
    try:
        ctx = ask_service.prepare(
            conn, KEYWORD, LORE, space="lore", embedder=None, dim=None
        )
    finally:
        conn.close()
    (snippet,) = [s for s in ctx.snippets if s.id == created["id"]]
    assert snippet.unverified
    assert "未審核" in ask_service.build_user_prompt("問", [snippet])
    assert snippet.source_dict()["review"] == "unverified"


def test_reject_pending_delete_restores_previous_state(rig):
    created = rig.uep_write()
    rig.review("approve", created["id"])
    rig.delete(rig.uep, created["id"])
    rej = rig.review("reject", created["id"], reason="不可刪")
    assert rej.json()["review_state"] == "verified"
    assert rig.get(rig.owner, created["id"])["review_state"] == "verified"


def test_ui_edit_of_pending_note_counts_as_approval(rig):
    created = rig.uep_write()
    resp = rig.ui.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": created["id"],
            "expected_updated": created["updated"],
            "title": "使用者修訂",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] == "verified"
    got = rig.get(rig.owner, created["id"])
    assert got["reviewed_by"] == "UEPBernie" and got["reviewed_at"]


def test_owner_bearer_update_of_pending_note_does_not_approve(rig):
    created = rig.uep_write()
    resp = rig.owner.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": created["id"],
            "expected_updated": created["updated"],
            "title": "bearer 修訂",
        },
    )
    assert resp.json()["review_state"] == "pending"


def test_dev_writes_take_effect_directly(rig):
    created = rig.uep_write(space="dev", vault=DEV)
    assert created["review_state"] == "none"
    resp = rig.uep.post(
        "/v1/update",
        json={
            "space": "dev",
            "vault": DEV,
            "id": created["id"],
            "expected_updated": created["updated"],
            "title": "改",
        },
    )
    assert resp.json()["review_state"] == "none"
    done = rig.delete(rig.uep, created["id"], space="dev", vault=DEV)
    assert done.json() == {
        "executed": True,
        "plan": done.json()["plan"],
        "review_state": "none",
    }
    gone = rig.owner.post(
        "/v1/get", json={"space": "dev", "vault": DEV, "ids": [created["id"]]}
    )
    assert gone.json()["missing"] == [created["id"]]


# ── list 篩選 ──


def test_list_filters_by_review_state(rig):
    pending = rig.uep_write("待審")
    verified = rig.uep_write("已核准", "別的內容")
    rig.review("approve", verified["id"])
    plain = write_note(rig.owner, LORE, "一般", "平常", space="lore")

    def ids(state):
        resp = rig.owner.post(
            "/v1/list",
            json={"space": "lore", "vault": LORE, "review_state": state},
        )
        assert resp.status_code == 200, resp.text
        return {i["id"]: i for i in resp.json()["items"]}

    assert set(ids("pending")) == {pending["id"]}
    assert ids("pending")[pending["id"]]["review"] == "unverified"
    assert set(ids("verified")) == {verified["id"]}
    assert "review" not in ids("verified")[verified["id"]]
    assert set(ids("none")) == {plain["id"]}
    bad = rig.owner.post(
        "/v1/list", json={"space": "lore", "vault": LORE, "review_state": "bogus"}
    )
    _error(bad, 400, "invalid_request")
    listed = rig.owner.post("/v1/list", json={"space": "lore", "vault": LORE})
    states = {i["id"]: i["review_state"] for i in listed.json()["items"]}
    assert states[pending["id"]] == "pending"


# ── 邊界 ──


@pytest.mark.parametrize("space", ["personal", "bogus"])
def test_uep_is_denied_outside_dev_and_lore(rig, space):
    note = write_note(rig.owner, PERSONAL, "日記", KEYWORD, space="personal")
    body = {"space": space, "vault": PERSONAL}
    calls = [
        ("/v1/vault_resolve", {"space": space, "key": PERSONAL}),
        ("/v1/recall", {**body, "query": KEYWORD}),
        ("/v1/ask", {**body, "question": KEYWORD}),
        ("/v1/get", {**body, "ids": [note["id"]]}),
        ("/v1/list", body),
        ("/v1/write", {**body, "title": "t", "body": "b"}),
        (
            "/v1/update",
            {
                **body,
                "id": note["id"],
                "expected_updated": note["updated"],
                "title": "改",
            },
        ),
        ("/v1/note_delete", {**body, "id": note["id"]}),
        ("/v1/status", body),
        ("/v1/document_download", {**body, "id": "doc:x"}),
    ]
    for path, payload in calls:
        _error(rig.uep.post(path, json=payload), 403, "uep_space_forbidden")


def test_space_guard_is_load_bearing(rig, monkeypatch):
    """拿掉 space 守衛，uep 就讀得到 personal。"""
    note = write_note(rig.owner, PERSONAL, "日記", KEYWORD, space="personal")
    payload = {"space": "personal", "vault": PERSONAL, "ids": [note["id"]]}
    _error(rig.uep.post("/v1/get", json=payload), 403, "uep_space_forbidden")
    monkeypatch.setattr(routes, "check_uep_space", lambda principal, space: None)
    assert rig.uep.post("/v1/get", json=payload).status_code == 200


def test_uep_cannot_undelete_upload_or_use_admin_endpoints(rig):
    created = rig.uep_write(space="dev", vault=DEV)
    rig.delete(rig.owner, created["id"], space="dev", vault=DEV)
    for path, payload in [
        ("/v1/note_undelete", {"space": "dev", "id": created["id"]}),
        ("/v1/tombstones", {"space": "dev", "vault": DEV}),
        ("/v1/vaults", {"space": "dev", "key": "folder/x", "display": "x"}),
        ("/v1/vault_delete", {"space": "dev", "key": DEV}),
        (
            "/v1/review/approve",
            {"space": "lore", "vault": LORE, "id": "x", "expected_updated": "x"},
        ),
        ("/v1/snapshot", {}),
        ("/v1/concept_graph", {"space": "dev", "vault": DEV, "scope": "x"}),
        ("/v1/concept_scopes", {"space": "dev", "vault": DEV}),
    ]:
        _error(rig.uep.post(path, json=payload), 403, "uep_endpoint_forbidden")
    upload = rig.uep.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"space": "dev", "vault": DEV},
    )
    _error(upload, 403, "uep_endpoint_forbidden")


def test_review_endpoints_reject_bearer_tokens(rig):
    created = rig.uep_write()
    for client in (rig.owner, rig.uep):
        for action, extra in (("approve", {}), ("reject", {"reason": "x"})):
            resp = client.post(
                f"/v1/review/{action}",
                json={
                    "space": "lore",
                    "vault": LORE,
                    "id": created["id"],
                    "expected_updated": created["updated"],
                    **extra,
                },
            )
            if client is rig.uep:
                _error(resp, 403, "uep_endpoint_forbidden")
            else:
                _error(resp, 403, "ui_session_required")
    assert rig.get(rig.owner, created["id"])["review_state"] == "pending"


def test_review_state_conflicts_are_409(rig):
    plain = write_note(rig.owner, LORE, "一般", "x", space="lore")
    _error(rig.review("approve", plain["id"]), 409, "review_state_conflict")
    _error(rig.review("reject", plain["id"], reason="x"), 409, "review_state_conflict")
    created = rig.uep_write()
    assert rig.review("approve", created["id"]).status_code == 200
    _error(rig.review("approve", created["id"]), 409, "review_state_conflict")
    _error(
        rig.review("reject", created["id"], reason="x"), 409, "review_state_conflict"
    )


def test_review_request_validates_space_and_vault(rig):
    created = rig.uep_write()
    # space 與 vault 不一致：note 不在該 space 的 vault 內
    wrong = rig.ui.post(
        "/v1/review/approve",
        json={
            "space": "dev",
            "vault": LORE,
            "id": created["id"],
            "expected_updated": created["updated"],
        },
    )
    assert wrong.status_code == 404, wrong.text
    missing_space = rig.ui.post(
        "/v1/review/approve",
        json={"vault": LORE, "id": created["id"], "expected_updated": "x"},
    )
    _error(missing_space, 400, "space_required")
    _error(rig.review("reject", created["id"], reason="  "), 400, "invalid_request")


# ── 設定 ──


def test_without_uep_token_nothing_changes(db_path):
    app = create_app(make_settings(db_path))
    with SpaceClient(app) as client:
        client.headers.update(AUTH)
        create_vault(client, LORE, space="lore")
        note = write_note(client, LORE, "一般", "內容", space="lore")
        assert note["review_state"] == "none"
        bad = SpaceClient(app)
        resp = bad.post(
            "/v1/get",
            json={"space": "lore", "vault": LORE, "ids": [note["id"]]},
            headers=UEP_AUTH,
        )
        assert resp.status_code == 401


def test_identical_tokens_refuse_to_start(db_path):
    with pytest.raises(ConfigError):
        create_app(make_settings(db_path, uep_token=Secret(AUTH_TOKEN())))


def AUTH_TOKEN() -> str:
    return AUTH["Authorization"].removeprefix("Bearer ")


def test_short_uep_token_refuses_to_start(db_path):
    with pytest.raises(ConfigError):
        create_app(make_settings(db_path, uep_token=Secret("short")))


# ── migration / 快照 ──


def test_migration_v19_defaults_existing_notes_to_none(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    raw.row_factory = sqlite3.Row
    try:
        assert migrate(raw, migrations=MIGRATIONS[:18]) == 18
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/a', 'a', 'repo', '2026-01-01T00:00:00.000Z')"
        )
        raw.execute(
            "INSERT INTO notes (id, vault, title, body, created, updated, principal, "
            "updated_by_principal) VALUES ('n1', 'folder/a', 't', 'b', "
            "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', 'owner', 'owner')"
        )
        assert migrate(raw) == SCHEMA_VERSION == 19
        row = raw.execute("SELECT * FROM notes WHERE id = 'n1'").fetchone()
        assert row["review_state"] == "none"
        assert row["reviewed_by"] is row["review_prev_state"] is None
    finally:
        raw.close()


def test_snapshot_carries_review_fields(rig, tmp_path):
    created = rig.uep_write()
    rig.review("approve", created["id"])
    dest = tmp_path / "snap.db"
    storage_snapshot.build_snapshot(rig.db_path, dest)
    conn = connect(dest)
    try:
        row = conn.execute(
            "SELECT review_state, reviewed_by FROM notes WHERE id = ?",
            (created["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert (row[0], row[1]) == ("verified", "UEPBernie")
    # 審核狀態變動要讓快照指紋改變（否則殼端拿到過期標註）
    before = storage_snapshot.source_fingerprint(rig.db_path)
    rig.delete(rig.uep, created["id"])
    assert storage_snapshot.source_fingerprint(rig.db_path) != before


# ── doctor：每條規則破壞後轉紅 ──


def _doctor(db_path) -> dict:
    conn = connect(db_path)
    try:
        report = default_registry().run(
            DoctorContext(settings={"embedding_dim": 16}, resources={"db": conn})
        )
    finally:
        conn.close()
    return next(c for c in report.to_dict()["checks"] if c["name"] == "notes.review")


def _corrupt(db_path, note_id: str, assignment: str) -> None:
    conn = connect(db_path)
    try:
        # 繞過欄位 CHECK，模擬舊程式或手動改庫
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE notes SET {assignment} WHERE id = ?", (note_id,))
    finally:
        conn.close()


def test_doctor_review_passes_on_healthy_data(rig):
    a = rig.uep_write("a")
    b = rig.uep_write("b", "另一篇")
    rig.review("approve", a["id"])
    rig.review("reject", b["id"], reason="x")
    rig.delete(rig.uep, a["id"])
    assert _doctor(rig.db_path)["status"] == "pass"


def test_doctor_flags_state_outside_whitelist(rig):
    note = rig.uep_write()
    _corrupt(rig.db_path, note["id"], "review_state = 'bogus'")
    assert _doctor(rig.db_path)["status"] == "fail"


def test_doctor_flags_pending_outside_lore(rig):
    note = write_note(rig.owner, DEV, "dev 筆記", "x", space="dev")
    assert _doctor(rig.db_path)["status"] == "pass"
    _corrupt(rig.db_path, note["id"], "review_state = 'pending'")
    assert _doctor(rig.db_path)["status"] == "fail"


def test_doctor_flags_verified_without_reviewer(rig):
    note = rig.uep_write()
    rig.review("approve", note["id"])
    assert _doctor(rig.db_path)["status"] == "pass"
    _corrupt(rig.db_path, note["id"], "reviewed_by = NULL")
    assert _doctor(rig.db_path)["status"] == "fail"


def test_doctor_flags_rejected_without_timestamp(rig):
    note = rig.uep_write()
    rig.review("reject", note["id"], reason="x")
    assert _doctor(rig.db_path)["status"] == "pass"
    _corrupt(rig.db_path, note["id"], "reviewed_at = NULL")
    assert _doctor(rig.db_path)["status"] == "fail"


def test_doctor_flags_pending_delete_without_prev_state(rig):
    note = rig.uep_write()
    rig.delete(rig.uep, note["id"])
    assert _doctor(rig.db_path)["status"] == "pass"
    _corrupt(rig.db_path, note["id"], "review_prev_state = NULL")
    assert _doctor(rig.db_path)["status"] == "fail"


def test_doctor_flags_stray_prev_state(rig):
    note = rig.uep_write()
    assert _doctor(rig.db_path)["status"] == "pass"
    _corrupt(rig.db_path, note["id"], "review_prev_state = 'verified'")
    assert _doctor(rig.db_path)["status"] == "fail"


# ── 審查後補強 ──


def _uep_update(rig, note, **fields):
    return rig.uep.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": note["id"],
            "expected_updated": note["updated"],
            **fields,
        },
    )


def test_m1_rejected_note_cannot_be_revived_by_delete_request(rig, monkeypatch):
    created = rig.uep_write()
    rig.review("reject", created["id"], reason="不實")
    body = {"space": "lore", "vault": LORE, "id": created["id"]}
    # 規劃階段就擋
    _error(rig.uep.post("/v1/note_delete", json=body), 409, "review_state_conflict")
    assert created["id"] not in rig.recall()
    # 拿掉保護：rejected 會以 pending_delete 重新出現在 recall
    monkeypatch.setattr(review_service, "assert_can_request_delete", lambda note: None)
    resp = rig.delete(rig.uep, created["id"])
    assert resp.json()["review_state"] == "pending_delete"
    assert created["id"] in rig.recall()


def test_m2_approve_requires_current_version(rig, monkeypatch):
    created = rig.uep_write()
    seen = rig.get(rig.owner, created["id"])["updated"]
    changed = _uep_update(rig, created, body=KEYWORD + " 偷換內容").json()
    stale = rig.review("approve", created["id"], expected_updated=seen)
    _error(stale, 409, "review_state_conflict")
    assert stale.json()["error"]["updated"] == changed["updated"]
    assert rig.get(rig.owner, created["id"])["review_state"] == "pending"
    _error(
        rig.review("reject", created["id"], expected_updated=seen, reason="x"),
        409,
        "review_state_conflict",
    )
    # 缺欄位是 422
    missing = rig.ui.post(
        "/v1/review/approve", json={"space": "lore", "vault": LORE, "id": created["id"]}
    )
    assert missing.status_code == 422
    # 拿掉版本檢查：使用者核准了沒看過的內容
    monkeypatch.setattr(review_service, "_check_version", lambda note, expected: None)
    assert (
        rig.review("approve", created["id"], expected_updated=seen).status_code == 200
    )


def test_minor1_uep_status_is_health_summary_only(rig):
    full = rig.owner.post("/v1/status", json={"space": "lore"}).json()
    assert "doctor" in full and "documents" in full
    summary = rig.uep.post("/v1/status", json={"space": "lore", "vault": LORE}).json()
    assert set(summary) == {"ok", "checked_at", "schema"}


def test_minor2_duplicates_ignore_rejected_notes(rig):
    first = rig.uep_write("重複標題", KEYWORD)
    again = write_note(rig.owner, LORE, "重複標題", KEYWORD, space="lore")
    assert [d["id"] for d in again["duplicates"]] == [first["id"]]
    rig.review("reject", first["id"], reason="x")
    third = write_note(rig.owner, LORE, "重複標題", KEYWORD, space="lore")
    assert first["id"] not in [d["id"] for d in third["duplicates"]]


def test_minor3_restore_after_approved_delete_keeps_prior_review_fields(rig):
    created = rig.uep_write()
    approved = rig.review("approve", created["id"]).json()
    rig.delete(rig.uep, created["id"])
    assert rig.review("approve", created["id"]).json()["deleted"] is True
    back = rig.ui.post("/v1/note_undelete", json={"space": "lore", "id": created["id"]})
    assert back.status_code == 200, back.text
    restored = rig.get(rig.owner, created["id"])
    assert restored["review_state"] == "verified"
    # 還原後的審核欄位是請求刪除前的那一組，不是刪除核准人的
    assert restored["reviewed_at"] == approved["reviewed_at"]
    assert restored["review_reason"] is None


def test_minor3_restore_of_unreviewed_note_has_no_reviewer(rig):
    plain = write_note(rig.owner, LORE, "一般", "x", space="lore")
    rig.delete(rig.uep, plain["id"])
    assert rig.review("approve", plain["id"]).json()["deleted"] is True
    rig.ui.post("/v1/note_undelete", json={"space": "lore", "id": plain["id"]})
    restored = rig.get(rig.owner, plain["id"])
    assert (restored["review_state"], restored["reviewed_by"]) == ("none", None)


def test_minor4_confirm_token_is_bound_to_principal(rig, monkeypatch):
    note = rig.uep_write(space="dev", vault=DEV)
    body = {"space": "dev", "vault": DEV, "id": note["id"]}
    plan = rig.owner.post("/v1/note_delete", json=body).json()
    stolen = {**body, "confirm_token": plan["confirm_token"]}
    _error(rig.uep.post("/v1/note_delete", json=stolen), 400, "invalid_confirm_token")
    # 拿掉 principal 綁定：uep 可以拿別人簽發的 token 執行
    monkeypatch.setattr(manage_api, "principal_of", lambda request: "same")
    plan = rig.owner.post("/v1/note_delete", json=body).json()
    resp = rig.uep.post(
        "/v1/note_delete", json={**body, "confirm_token": plan["confirm_token"]}
    )
    assert resp.status_code == 200 and resp.json()["executed"] is True


def test_minor5_ui_username_uep_is_reserved():
    from lore_vault.storage import ui_login

    for name in ("uep", "UEP", "Uep"):
        with pytest.raises(ui_login.AccountError):
            ui_login.validate_username(name)
    assert ui_login.validate_username("uep2") == "uep2"


def test_minor6_uep_cannot_overwrite_notes_edited_by_the_user(rig, monkeypatch):
    created = rig.uep_write()
    edited = rig.ui.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": created["id"],
            "expected_updated": created["updated"],
            "title": "使用者的修訂",
        },
    ).json()
    latest = {"id": created["id"], "updated": edited["updated"]}
    _error(_uep_update(rig, latest, body="蓋掉"), 403, "uep_not_author")
    assert rig.get(rig.owner, created["id"])["title"] == "使用者的修訂"
    # 拿掉保護：uep 蓋掉使用者的編輯，且 note 回到 pending
    monkeypatch.setattr(notes_service_mod, "check_uep_owns", lambda *a, **k: None)
    assert _uep_update(rig, latest, body="蓋掉").status_code == 200
    assert rig.get(rig.owner, created["id"])["review_state"] == "pending"


# ── /mcp ──


class McpRpc:
    def __init__(self, client: TestClient, headers: dict[str, str]):
        self.client = client
        self.headers = {"Accept": "application/json, text/event-stream", **headers}
        self._id = 0
        resp = self._send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        assert resp.status_code == 200, resp.text
        self.headers["mcp-session-id"] = resp.headers["mcp-session-id"]
        self.headers["mcp-protocol-version"] = "2025-06-18"
        note = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self.headers,
        )
        assert note.status_code == 202, note.text

    def _send(self, method, params):
        self._id += 1
        return self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            headers=self.headers,
        )

    def call(self, name, **args):
        result = self._send("tools/call", {"name": name, "arguments": args})
        assert result.status_code == 200, result.text
        body = result.json()["result"]
        return bool(body.get("isError")), body["content"][0]["text"]


def test_uep_over_mcp_allowed_tools_work_and_others_are_forbidden(db_path):
    # /mcp 的 session manager 一個 app 只能啟動一次：這裡只開一個 TestClient
    app = create_app(make_settings(db_path, uep_token=Secret(UEP_TOKEN)))
    with TestClient(app) as client:
        client.headers.update(AUTH)
        create_vault(client, LORE, space="lore")
        create_vault(client, PERSONAL, space="personal")
        mcp = McpRpc(client, UEP_AUTH)
        assert mcp.call("space", action="set", value="lore")[0] is False
        is_error, text = mcp.call("write", vault=LORE, title="經 MCP", body=KEYWORD)
        assert not is_error, text
        assert '"review_state":"pending"' in text.replace(" ", "")
        # 不在允許清單的端點（上傳、undelete）
        is_error, text = mcp.call(
            "upload", vault=LORE, filename="a.txt", content_base64="aGVsbG8="
        )
        assert is_error and "uep_endpoint_forbidden" in text
        is_error, text = mcp.call("undelete", id="x")
        assert is_error and "uep_endpoint_forbidden" in text
        # personal
        assert mcp.call("space", action="set", value="personal")[0] is False
        is_error, text = mcp.call("recall", vault=PERSONAL, query=KEYWORD)
        assert is_error and "uep_space_forbidden" in text


def test_uep_endpoint_allowlist_is_load_bearing(rig, monkeypatch):
    monkeypatch.setattr(auth_mod, "UEP_ALLOWED_PATHS", frozenset({"/v1/note_undelete"}))
    # 清單被改掉後，原本允許的端點也被擋 → 證明清單真的在把關
    _error(
        rig.uep.post("/v1/recall", json={"space": "lore", "vault": LORE, "query": "x"}),
        403,
        "uep_endpoint_forbidden",
    )
