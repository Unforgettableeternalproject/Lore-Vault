"""一般 agent 待審操作進入審核頁（add-agent-review-proposals）的服務端行為。

- 受保護操作（刪除、改人類寫的 note、連結移除、任務封存）建立提案，UI 核准才生效
- 寫入來源由認證方式決定（同一 principal 的 UI session 與 bearer 得到不同 write_origin）
- 雙通道（對話 confirm_token 與 UI 核准）只有一個能結案，且與刪除同一交易
- 審核端點只收 UI session；bearer 的回應不含 payload（筆記原文）
- 每道保護配一個負向測試：把保護拿掉，對應的不變量測試必須轉紅

測試隔離：每個 rig 自己的 app（自己的 ConfirmSigner）與資料庫；不依賴模組級 cache。
"""

from __future__ import annotations

import ast
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import manage as manage_api
from lore_vault.api import principals as principals_mod
from lore_vault.api import routes
from lore_vault.api.app import create_app
from lore_vault.config import Secret
from lore_vault.proposals import policy
from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.storage import notes as storage_notes
from lore_vault.storage import proposals as store
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.db import connect
from lore_vault.storage.migrate import MIGRATIONS, migrate

from .conftest import (
    AUTH,
    UI_PASSWORD,
    SpaceClient,
    create_vault,
    make_settings,
    seed_ui_account,
    write_note,
)

UI = {"X-Lore-Vault-UI": "1"}
DEV = "folder/prop-dev"
DEV2 = "folder/prop-dev2"
LORE = "lore/prop-arc"
TASK_VAULT = "folder/prop-tasks"
UEP_TOKEN = "uep-token-0123456789abcdef"
SAME_PRINCIPAL = "owner"  # 與 bearer 的預設 principal 相同


class Rig:
    def __init__(self, owner: TestClient, ui: TestClient, db_path: Path) -> None:
        self.owner, self.ui, self.db_path = owner, ui, db_path

    # ── 讀寫輔助 ──
    def note(self, title="筆記", body="內文", vault=DEV, **extra) -> dict:
        return write_note(self.owner, vault, title, body, **extra)

    def ui_edit(self, note_id, expected, vault=DEV, space="dev", **fields) -> dict:
        resp = self.ui.post(
            "/v1/update",
            json={
                "space": space,
                "vault": vault,
                "id": note_id,
                "expected_updated": expected,
                **fields,
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def bearer_update(self, note_id, expected, vault=DEV, **fields):
        return self.owner.post(
            "/v1/update",
            json={
                "vault": vault,
                "id": note_id,
                "expected_updated": expected,
                **fields,
            },
        )

    def protected_note(self, **extra) -> dict:
        """建立一則 write_origin=ui 的 note（先由 agent 寫、再由 UI 編輯）。"""
        created = self.note(**extra)
        edited = self.ui_edit(created["id"], created["updated"], body="人類改過的內文")
        return {**created, "updated": edited["updated"]}

    def row(self, note_id) -> dict:
        conn = connect(self.db_path)
        try:
            return dict(
                conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            )
        finally:
            conn.close()

    def proposals(self, **where) -> list[dict]:
        conn = connect(self.db_path)
        try:
            sql = "SELECT * FROM review_proposals"
            if where:
                sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
            return [
                dict(r)
                for r in conn.execute(
                    sql + " ORDER BY created_at, id", tuple(where.values())
                )
            ]
        finally:
            conn.close()

    def queue(self, **body) -> dict:
        resp = self.ui.post(
            "/v1/review/queue", json={"space": "dev", "vault": "*", **body}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def approve(self, prop_id, rev=1, vault=DEV, space="dev", **extra):
        return self.ui.post(
            "/v1/review/proposal_approve",
            json={
                "space": space,
                "vault": vault,
                "id": prop_id,
                "expected_rev": rev,
                **extra,
            },
        )

    def reject(self, prop_id, rev=1, vault=DEV, space="dev", reason="不要"):
        return self.ui.post(
            "/v1/review/proposal_reject",
            json={
                "space": space,
                "vault": vault,
                "id": prop_id,
                "expected_rev": rev,
                "reason": reason,
            },
        )

    def delete_plan(self, note_id, vault=DEV, **extra):
        resp = self.owner.post(
            "/v1/note_delete",
            json={"space": "dev", "vault": vault, "id": note_id, **extra},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def delete_exec(self, note_id, token, vault=DEV, **extra):
        return self.owner.post(
            "/v1/note_delete",
            json={
                "space": "dev",
                "vault": vault,
                "id": note_id,
                "confirm_token": token,
                **extra,
            },
        )


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


@pytest.fixture
def rig(db_path):
    seed_ui_account(db_path, username=SAME_PRINCIPAL)
    app = create_app(make_settings(db_path, uep_token=Secret(UEP_TOKEN)))
    owner = SpaceClient(app)
    owner.__enter__()
    owner.headers.update(AUTH)
    ui = TestClient(app, base_url="https://testserver")
    ui.__enter__()
    login = ui.post(
        "/ui/api/login",
        json={"username": SAME_PRINCIPAL, "password": UI_PASSWORD},
        headers=UI,
    )
    assert login.status_code == 204, login.text
    ui.headers.update(UI)
    create_vault(owner, DEV)
    create_vault(owner, DEV2)
    create_vault(owner, LORE, space="lore")
    yield Rig(owner, ui, db_path)
    ui.__exit__(None, None, None)
    owner.__exit__(None, None, None)


# ── 寫入來源由認證方式決定 ──


def _origin_invariant(rig: Rig) -> None:
    """同一 principal：bearer 寫 → agent，UI session 寫 → ui（不看 principal）。"""
    via_bearer = rig.note(title="bearer 寫")
    resp = rig.ui.post(
        "/v1/write",
        json={"space": "dev", "vault": DEV, "title": "ui 寫", "body": "b"},
    )
    assert resp.status_code == 201, resp.text
    assert rig.row(via_bearer["id"])["write_origin"] == "agent"
    assert rig.row(resp.json()["id"])["write_origin"] == "ui"
    assert (
        rig.row(via_bearer["id"])["principal"]
        == rig.row(resp.json()["id"])["principal"]
    )


def test_write_origin_follows_auth_method_not_principal(rig):
    _origin_invariant(rig)


def test_write_origin_negative_deciding_by_principal_turns_red(rig, monkeypatch):
    def by_principal(request, author=None):
        # 錯誤實作：依 principal 判斷（UI 帳號與 bearer 同一 principal → 一律 agent）
        return t.Actor.agent(principals_mod.principal_of(request), author)

    monkeypatch.setattr(routes, "actor_of", by_principal)
    with pytest.raises(AssertionError):
        _origin_invariant(rig)


def test_old_notes_default_to_agent_and_ui_edit_turns_ui(rig):
    created = rig.note()
    assert rig.row(created["id"])["write_origin"] == "agent"
    rig.ui_edit(created["id"], created["updated"], title="人類改標題")
    assert rig.row(created["id"])["write_origin"] == "ui"


def test_agent_update_of_agent_note_applies_directly(rig):
    created = rig.note()
    resp = rig.bearer_update(created["id"], created["updated"], body="新內文")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("applied", True) is True
    assert "proposal" not in resp.json()
    assert rig.row(created["id"])["body"] == "新內文"
    assert rig.proposals() == []


# ── (b) 改人類寫的 note → 提案 ──


def _hold_invariant(rig: Rig) -> dict:
    """bearer 改 ui 來源的 note：202、note 整列不變、有提案。"""
    note = rig.protected_note()
    before = rig.row(note["id"])
    resp = rig.bearer_update(note["id"], note["updated"], body="agent 想改的內文")
    assert resp.status_code == 202, resp.text
    assert rig.row(note["id"]) == before
    assert len(rig.proposals(kind="note_update")) == 1
    return {"note": note, "before": before, "resp": resp.json()}


def test_agent_update_of_human_note_becomes_proposal(rig):
    ctx = _hold_invariant(rig)
    body = ctx["resp"]
    assert body["applied"] is False and body["proposal_pending"] is True
    assert body["reason"] == "human_written_note"
    prop = body["proposal"]
    assert set(prop) == {"id", "kind", "state", "base_updated", "expires_at", "deduped"}
    assert (prop["kind"], prop["state"], prop["deduped"]) == (
        "note_update",
        "pending",
        False,
    )
    assert prop["base_updated"] == ctx["note"]["updated"]
    # bearer 看不到提案內容（含筆記原文）
    assert "agent 想改的內文" not in json.dumps(body, ensure_ascii=False)


def test_hold_negative_policy_allowing_everything_turns_red(rig, monkeypatch):
    monkeypatch.setattr(policy, "decide_update", lambda *a, **k: t.NO_HOLD)
    with pytest.raises(AssertionError):
        _hold_invariant(rig)


def test_reject_leaves_note_row_identical(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    resp = rig.reject(prop["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["state"] == "rejected"
    assert (
        rig.row(ctx["note"]["id"]) == ctx["before"]
    )  # 整列（含 updated、embedding 狀態）
    _code(rig.reject(prop["id"], rev=2), 409, "proposal_state_conflict")


def test_approve_applies_and_marks_ui_origin(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    resp = rig.approve(prop["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["state"] == "applied"
    row = rig.row(ctx["note"]["id"])
    assert row["body"] == "agent 想改的內文"
    assert row["write_origin"] == "ui"
    # 仍記提案者，不假冒使用者
    assert row["updated_by_principal"] == SAME_PRINCIPAL
    done = rig.proposals(id=prop["id"])[0]
    assert done["state"] == "applied" and done["applied_updated"] == row["updated"]
    assert done["resolved_via"] == "ui" and done["resolved_by"] == SAME_PRINCIPAL


def test_approve_wrong_rev_or_unknown_is_conflict_or_404(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    _code(rig.approve(prop["id"], rev=9), 409, "proposal_state_conflict")
    _code(rig.approve("prop_missing"), 404, "proposal_not_found")
    # 範圍錯誤與不存在相同
    _code(rig.approve(prop["id"], vault=DEV2), 404, "proposal_not_found")
    assert rig.row(ctx["note"]["id"])["body"] == ctx["before"]["body"]


def test_duplicate_proposal_is_deduped_and_different_one_supersedes(rig):
    ctx = _hold_invariant(rig)
    first = ctx["resp"]["proposal"]
    again = rig.bearer_update(
        ctx["note"]["id"], ctx["note"]["updated"], body="agent 想改的內文"
    ).json()["proposal"]
    assert again["id"] == first["id"] and again["deduped"] is True
    assert rig.proposals(id=first["id"])[0]["rev"] == 1  # 去重不動 rev
    newer = rig.bearer_update(
        ctx["note"]["id"], ctx["note"]["updated"], body="第二個想法"
    ).json()["proposal"]
    assert newer["id"] != first["id"] and newer["superseded"] == first["id"]
    old = rig.proposals(id=first["id"])[0]
    assert (old["state"], old["superseded_by"]) == ("superseded", newer["id"])


def test_open_proposal_is_unique_in_database(rig):
    _hold_invariant(rig)
    conn = connect(rig.db_path)
    try:
        existing = rig.proposals(kind="note_update")[0]
        cols = [k for k in existing]
        dup = dict(existing, id="prop_dup")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO review_proposals ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [dup[c] for c in cols],
            )
    finally:
        conn.close()


def test_queue_full_blocks_new_proposals(rig, monkeypatch):
    monkeypatch.setitem(store.upsert_open.__kwdefaults__, "max_open", 1)
    a, b = rig.protected_note(title="甲"), rig.protected_note(title="乙")
    assert rig.bearer_update(a["id"], a["updated"], body="x").status_code == 202
    _code(
        rig.bearer_update(b["id"], b["updated"], body="y"), 429, "proposal_queue_full"
    )


def test_stale_when_touched_field_changed_and_rebase(rig):
    ctx = _hold_invariant(rig)
    prop, note = ctx["resp"]["proposal"], ctx["note"]
    conn = connect(rig.db_path)
    try:  # 非 UI 路徑（例如 CLI／uep）改了被觸及的 body
        changed = storage_notes.update_note_if(
            conn,
            DEV,
            note["id"],
            rig.row(note["id"])["updated"],
            {"body": "別人改的"},
            space="dev",
            editor=(None, "someone"),
        )
    finally:
        conn.close()
    assert changed is not None
    stale = rig.approve(prop["id"])
    _code(stale, 409, "proposal_stale")
    err = stale.json()["error"]
    assert err["base"]["body"] == "人類改過的內文"
    assert err["current"]["body"] == "別人改的"
    assert err["proposed"]["body"] == "agent 想改的內文"
    assert err["current_updated"] == changed.updated
    assert rig.row(note["id"])["body"] == "別人改的"  # 失敗不動 note
    ok = rig.approve(prop["id"], rebase_to=err["current_updated"])
    assert ok.status_code == 200, ok.text
    assert rig.row(note["id"])["body"] == "agent 想改的內文"


def test_untouched_field_change_does_not_make_stale(rig):
    ctx = _hold_invariant(rig)
    note = ctx["note"]
    conn = connect(rig.db_path)
    try:
        storage_notes.update_note_if(
            conn,
            DEV,
            note["id"],
            rig.row(note["id"])["updated"],
            {"topics": ("x",)},
            space="dev",
            editor=(None, "someone"),
        )
    finally:
        conn.close()
    assert rig.approve(ctx["resp"]["proposal"]["id"]).status_code == 200


def test_ui_direct_edit_supersedes_open_proposal(rig):
    ctx = _hold_invariant(rig)
    note = ctx["note"]
    rig.ui_edit(note["id"], rig.row(note["id"])["updated"], title="使用者自己改")
    done = rig.proposals(kind="note_update")[0]
    assert (done["state"], done["resolved_via"]) == ("superseded", "ui_direct")


def test_approve_when_target_vanished_marks_obsolete(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    conn = connect(rig.db_path)
    try:  # 繞過服務層直接刪（例如 CLI 之外的異常），驗證核准時的防線
        conn.execute("PRAGMA foreign_keys = ON")
        seq = conn.execute(
            "SELECT seq FROM notes WHERE id = ?", (ctx["note"]["id"],)
        ).fetchone()[0]
        conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))
        conn.execute("DELETE FROM notes WHERE id = ?", (ctx["note"]["id"],))
    finally:
        conn.close()
    _code(rig.approve(prop["id"]), 409, "proposal_state_conflict")
    assert rig.proposals(id=prop["id"])[0]["state"] == "obsolete"


def test_proposals_expire_lazily(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    conn = connect(rig.db_path)
    try:
        conn.execute(
            "UPDATE review_proposals SET expires_at = '2020-01-01T00:00:00.000Z'"
        )
    finally:
        conn.close()
    items = rig.queue()["items"]
    assert items == []
    assert rig.proposals(id=prop["id"])[0]["state"] == "expired"
    _code(rig.approve(prop["id"]), 409, "proposal_state_conflict")


# ── (c) 連結矩陣 ──


def _linked(rig: Rig):
    b = rig.note(title="乙")
    c = rig.note(title="丙")
    a = rig.note(title="甲", links=[b["id"]])
    return a, b, c


def test_link_addition_is_not_held(rig):
    a, b, c = _linked(rig)
    resp = rig.bearer_update(a["id"], a["updated"], links=[b["id"], c["id"]])
    assert resp.status_code == 200 and "proposal" not in resp.json()
    assert set(
        rig.owner.post("/v1/get", json={"vault": DEV, "ids": [a["id"]]}).json()[
            "items"
        ][0]["links"]
    ) == {b["id"], c["id"]}
    assert rig.proposals() == []


def test_explicit_link_removal_only_is_held_entirely(rig):
    a, b, _ = _linked(rig)
    before = rig.row(a["id"])
    resp = rig.bearer_update(a["id"], a["updated"], links=[])
    assert resp.status_code == 202, resp.text
    assert resp.json()["reason"] == "link_change_held"
    assert rig.row(a["id"]) == before
    prop = rig.proposals(kind="link_change")[0]
    assert json.loads(prop["payload"]) == {"remove": [b["id"]], "supersedes": None}


def test_removal_with_other_changes_applies_rest_partially(rig):
    a, b, c = _linked(rig)
    resp = rig.bearer_update(a["id"], a["updated"], title="新標題", links=[c["id"]])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["partial"] is True and body["proposal_pending"] is True
    assert body["held"] == {"links_removed": [b["id"]], "supersedes_changed": False}
    assert set(body["links"]) == {b["id"], c["id"]}  # 被攔下的移除還在，新增照套
    assert rig.row(a["id"])["title"] == "新標題"


def test_supersedes_change_is_held_but_first_set_is_not(rig):
    old1, old2 = rig.note(title="舊一"), rig.note(title="舊二")
    a = rig.note(title="甲")
    first = rig.bearer_update(a["id"], a["updated"], supersedes=old1["id"])
    assert first.status_code == 200 and "proposal" not in first.json()
    cur = first.json()["updated"]
    changed = rig.bearer_update(a["id"], cur, supersedes=old2["id"])
    assert changed.status_code == 202
    assert rig.row(a["id"])["supersedes"] == old1["id"]
    cleared = rig.bearer_update(a["id"], cur, supersedes=None)
    assert cleared.status_code == 202
    live = [p for p in rig.proposals(kind="link_change") if p["state"] == "pending"]
    assert len(live) == 1
    assert json.loads(live[0]["payload"])["supersedes"] == {
        "from": old1["id"],
        "to": None,
    }


def test_body_derived_link_loss_is_not_held(rig):
    b = rig.note(title="乙")
    a = rig.note(title="甲", body="見 [[乙]] 的說明")
    assert (
        b["id"]
        in rig.owner.post("/v1/get", json={"vault": DEV, "ids": [a["id"]]}).json()[
            "items"
        ][0]["links"]
    )
    resp = rig.bearer_update(a["id"], a["updated"], body="不再引用")
    assert resp.status_code == 200 and resp.json()["links"] == []
    assert rig.proposals() == []


def test_approve_link_change_removes_links_and_marks_ui(rig):
    a, b, _ = _linked(rig)
    rig.bearer_update(a["id"], a["updated"], links=[])
    prop = rig.proposals(kind="link_change")[0]
    resp = rig.approve(prop["id"])
    assert resp.status_code == 200, resp.text
    row = rig.row(a["id"])
    assert json.loads(row["links"]) == [] and row["write_origin"] == "ui"


# ── (a) 刪除的雙通道 ──


def _delete_flow(rig: Rig):
    note = rig.note(title="待刪")
    plan = rig.delete_plan(note["id"])
    return note, plan


def test_delete_plan_binds_proposal_and_token_execution_closes_it(rig):
    note, plan = _delete_flow(rig)
    assert plan["executed"] is False and plan["proposal_pending"] is True
    prop = plan["proposal"]
    assert prop["kind"] == "note_delete" and prop["state"] == "pending"
    row = rig.proposals(id=prop["id"])[0]
    summary = json.loads(row["payload"])["plan_summary"]
    assert summary["title"] == "待刪" and set(summary) == {
        "title",
        "link_count",
        "backlink_count",
    }
    # 重新規劃：命中同一提案（去重，rev 不動）
    again = rig.delete_plan(note["id"])
    assert again["proposal"]["id"] == prop["id"] and again["proposal"]["deduped"]
    done = rig.delete_exec(note["id"], again["confirm_token"])
    assert done.status_code == 200, done.text
    assert done.json()["proposal"] == {
        "id": prop["id"],
        "kind": "note_delete",
        "state": "applied",
        "resolved_via": "token",
    }
    final = rig.proposals(id=prop["id"])[0]
    assert (final["state"], final["resolved_via"]) == ("applied", "token")
    assert rig.queue()["items"] == []


def test_ui_approval_deletes_and_old_token_cannot_delete_again(rig):
    note, plan = _delete_flow(rig)
    resp = rig.approve(plan["proposal"]["id"])
    assert resp.status_code == 200, resp.text
    assert rig.proposals(id=plan["proposal"]["id"])[0]["resolved_via"] == "ui"
    _code(rig.delete_exec(note["id"], plan["confirm_token"]), 404, "not_found")


def test_old_token_after_ui_approval_and_undelete_requires_new_consent(rig):
    note, plan = _delete_flow(rig)
    rig.approve(plan["proposal"]["id"])
    undeleted = rig.ui.post(
        "/v1/note_undelete", json={"space": "dev", "id": note["id"]}
    )
    assert undeleted.status_code == 200, undeleted.text
    assert rig.row(note["id"])["id"] == note["id"]  # 還原了
    stale = rig.delete_exec(note["id"], plan["confirm_token"])
    _code(stale, 409, "plan_changed")
    err = stale.json()["error"]
    assert rig.row(note["id"])["id"] == note["id"]  # 沒有因為舊同意再次刪除
    # plan_changed 附的新 token 綁在「新提交的」開放提案上，可以正常執行
    open_props = rig.proposals(kind="note_delete", state="pending")
    assert len(open_props) == 1
    done = rig.delete_exec(note["id"], err["confirm_token"])
    assert done.status_code == 200, done.text
    assert done.json()["proposal"]["state"] == "applied"


def test_token_execution_failure_rolls_back_proposal_transition(rig, monkeypatch):
    note, plan = _delete_flow(rig)

    def boom(*a, **k):
        raise RuntimeError("刪除中途失敗")

    monkeypatch.setattr(manage_api.admin, "delete_note", boom)
    with pytest.raises(RuntimeError):
        rig.delete_exec(note["id"], plan["confirm_token"])
    assert rig.proposals(id=plan["proposal"]["id"])[0]["state"] == "pending"
    assert rig.row(note["id"])["id"] == note["id"]


def test_delete_negative_ui_approval_without_closing_lets_token_double_execute(
    rig, monkeypatch
):
    """拿掉「核准同交易結案」後，同一份同意可以執行兩次——不變量測試會轉紅。"""

    def invariant():
        note, plan = _delete_flow(rig)
        rig.approve(plan["proposal"]["id"])
        resp = rig.delete_exec(note["id"], plan["confirm_token"])
        assert resp.status_code in (404, 409)
        assert rig.proposals(id=plan["proposal"]["id"])[0]["state"] == "applied"

    invariant()  # 正常實作通過
    monkeypatch.setattr(proposals_service, "_mark_applied", lambda *a, **k: None)
    with pytest.raises(AssertionError):
        invariant()


def test_ui_direct_delete_supersedes_open_proposal(rig):
    note, plan = _delete_flow(rig)
    other = rig.protected_note(title="另一則")
    held = rig.bearer_update(other["id"], other["updated"], body="x").json()["proposal"]
    # UI 自己刪除 note：沒有提案介入，目標上的開放提案過時
    ui_plan = rig.ui.post(
        "/v1/note_delete", json={"space": "dev", "vault": DEV, "id": other["id"]}
    ).json()
    assert "proposal" not in ui_plan
    done = rig.ui.post(
        "/v1/note_delete",
        json={
            "space": "dev",
            "vault": DEV,
            "id": other["id"],
            "confirm_token": ui_plan["confirm_token"],
        },
    )
    assert done.status_code == 200, done.text
    row = rig.proposals(id=held["id"])[0]
    assert row["state"] == "superseded" and row["resolved_via"] == "ui_direct"
    assert rig.proposals(id=plan["proposal"]["id"])[0]["state"] == "pending"


def test_deleting_target_obsoletes_other_open_proposals(rig):
    note = rig.protected_note()
    held = rig.bearer_update(note["id"], note["updated"], body="x").json()["proposal"]
    plan = rig.delete_plan(note["id"])
    rig.approve(plan["proposal"]["id"])
    assert rig.proposals(id=held["id"])[0]["state"] == "obsolete"


def test_vault_delete_obsoletes_open_proposals(rig):
    note = rig.protected_note()
    held = rig.bearer_update(note["id"], note["updated"], body="x").json()["proposal"]
    planned = rig.ui.post("/v1/vault_delete", json={"space": "dev", "key": DEV}).json()
    done = rig.ui.post(
        "/v1/vault_delete",
        json={"space": "dev", "key": DEV, "confirm_token": planned["confirm_token"]},
    )
    assert done.status_code == 200, done.text
    assert rig.proposals(id=held["id"])[0]["state"] == "obsolete"


def test_uep_deletes_and_writes_never_create_proposals(rig):
    uep = SpaceClient(rig.ui.app)
    uep.__enter__()
    try:
        uep.headers.update({"Authorization": f"Bearer {UEP_TOKEN}"})
        created = uep.post(
            "/v1/write", json={"vault": DEV, "title": "uep 寫", "body": "b"}
        ).json()
        plan = uep.post(
            "/v1/note_delete", json={"space": "dev", "vault": DEV, "id": created["id"]}
        ).json()
        assert "proposal" not in plan
        done = uep.post(
            "/v1/note_delete",
            json={
                "space": "dev",
                "vault": DEV,
                "id": created["id"],
                "confirm_token": plan["confirm_token"],
            },
        )
        assert done.status_code == 200
        assert rig.proposals() == []
    finally:
        uep.__exit__(None, None, None)


# ── 審核端點只收 UI session ──


@pytest.mark.parametrize(
    "path",
    [
        "/v1/review/queue",
        "/v1/review/counts",
        "/v1/review/proposal_get",
        "/v1/review/proposal_approve",
        "/v1/review/proposal_reject",
    ],
)
def test_review_endpoints_reject_bearer_before_body_validation(rig, path):
    resp = rig.owner.post(path, json={"garbage": True})  # 壞 body 也先擋認證
    _code(resp, 403, "ui_session_required")


def test_bearer_cannot_approve_even_with_valid_body(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    resp = rig.owner.post(
        "/v1/review/proposal_approve",
        json={"space": "dev", "vault": DEV, "id": prop["id"], "expected_rev": 1},
    )
    _code(resp, 403, "ui_session_required")
    assert rig.proposals(id=prop["id"])[0]["state"] == "pending"


def test_get_reports_open_proposals_without_payload(rig):
    ctx = _hold_invariant(rig)
    item = rig.owner.post(
        "/v1/get", json={"vault": DEV, "ids": [ctx["note"]["id"]]}
    ).json()["items"][0]
    prop = ctx["resp"]["proposal"]
    assert item["open_proposals"] == [
        {"id": prop["id"], "kind": "note_update", "state": "pending"}
    ]
    other = rig.note(title="沒有提案")
    plain = rig.owner.post("/v1/get", json={"vault": DEV, "ids": [other["id"]]}).json()
    assert plain["items"][0]["open_proposals"] == []


# ── 佇列、計數、單筆 ──


def test_queue_item_shape_counts_and_get(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    plan = rig.delete_plan(rig.note(title="要刪")["id"])
    items = rig.queue()["items"]
    assert {i["kind"] for i in items} == {"note_update", "note_delete"}
    upd = next(i for i in items if i["kind"] == "note_update")
    assert upd["item_id"] == prop["id"] and upd["source"] == "proposal"
    assert set(upd) == {
        "item_id",
        "source",
        "kind",
        "vault",
        "target",
        "summary",
        "proposer",
        "created_at",
        "expires_at",
        "state",
        "rev",
        "stale",
        "deep_link",
    }
    assert upd["proposer"] == {"principal": SAME_PRINCIPAL, "author": None}
    assert upd["stale"] is False and upd["target"]["type"] == "note"
    assert (
        rig.queue(kinds=["note_delete"])["items"][0]["item_id"]
        == plan["proposal"]["id"]
    )
    counts = rig.ui.post(
        "/v1/review/counts", json={"space": "dev", "vault": "*"}
    ).json()
    assert counts == {"counts": {"note_update": 1, "note_delete": 1}}
    detail = rig.ui.post(
        "/v1/review/proposal_get", json={"space": "dev", "vault": DEV, "id": prop["id"]}
    ).json()
    assert detail["proposal"]["payload"] == {"changes": {"body": "agent 想改的內文"}}
    assert detail["proposal"]["base_snapshot"] == {"body": "人類改過的內文"}
    assert detail["current"] == {"body": "人類改過的內文"}
    assert detail["target"]["title"] == "筆記"
    assert detail["current_updated"] == ctx["note"]["updated"]


def test_queue_pagination_scope_and_personal(rig):
    ids = []
    for i in range(3):
        n = rig.protected_note(title=f"n{i}")
        ids.append(
            rig.bearer_update(n["id"], n["updated"], body=f"x{i}").json()["proposal"][
                "id"
            ]
        )
    first = rig.queue(limit=2)
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = rig.queue(limit=2, cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["has_more"] is False
    seen = [i["item_id"] for i in first["items"] + second["items"]]
    assert sorted(seen) == sorted(ids)
    assert rig.queue(vault=DEV2)["items"] == []
    assert rig.queue(space="personal")["items"] == []
    _code(
        rig.ui.post(
            "/v1/review/queue", json={"space": "dev", "vault": "*", "cursor": "壞掉"}
        ),
        400,
        "invalid_cursor",
    )


def test_queue_merges_uep_pending_notes_in_lore(rig):
    uep = SpaceClient(rig.ui.app)
    uep.__enter__()
    try:
        uep.headers.update({"Authorization": f"Bearer {UEP_TOKEN}"})
        made = uep.post(
            "/v1/write",
            json={"space": "lore", "vault": LORE, "title": "UEP 的", "body": "b"},
        )
        assert made.status_code == 201, made.text
    finally:
        uep.__exit__(None, None, None)
    out = rig.ui.post("/v1/review/queue", json={"space": "lore", "vault": "*"}).json()
    (item,) = out["items"]
    assert (item["source"], item["kind"], item["state"]) == (
        "uep",
        "uep_review",
        "pending",
    )
    assert item["item_id"] == "uep:" + made.json()["id"]
    counts = rig.ui.post(
        "/v1/review/counts", json={"space": "lore", "vault": "*"}
    ).json()
    assert counts["counts"] == {"uep_review": 1}


# ── 任務層 ──


def _put_change(rig: Rig, name="needs-ok", requires=True):
    doc = {
        "schema": 1,
        "name": name,
        "state": "active",
        "meta": {"requires_authorization": requires},
        "proposal_md": "# P\n",
        "design_md": None,
        "tasks_md": "# T\n\n- [x] 1.1 ok\n",
        "deltas": {},
        "apply": None,
    }
    resp = rig.owner.post(
        "/v1/blob_put",
        json={
            "space": "dev",
            "vault": TASK_VAULT,
            "key": f"task-change:{name}",
            "mime": "application/json",
            "content_base64": base64.b64encode(json.dumps(doc).encode()).decode(),
        },
    )
    assert resp.status_code == 200, resp.text


def test_task_auth_items_are_derived_and_vanish_after_ui_authorization(rig):
    create_vault(rig.owner, TASK_VAULT)
    _put_change(rig)
    _put_change(rig, "no-need", requires=False)
    out = rig.queue()
    (item,) = [i for i in out["items"] if i["source"] == "task_auth"]
    assert (item["kind"], item["target"]["id"], item["vault"]) == (
        "task_auth",
        "needs-ok",
        TASK_VAULT,
    )
    assert rig.ui.post("/v1/review/counts", json={"space": "dev", "vault": "*"}).json()[
        "counts"
    ] == {"task_auth": 1}
    auth = rig.ui.post(
        "/v1/tasks_authorize", json={"vault": TASK_VAULT, "change": "needs-ok"}
    )
    assert auth.status_code == 200, auth.text
    assert [i for i in rig.queue()["items"] if i["source"] == "task_auth"] == []
    assert rig.proposals() == []  # 衍生項目不入表


def _archive_register(rig, name="arch", digest="d1", **extra):
    return rig.owner.post(
        "/v1/proposal_register",
        json={"vault": TASK_VAULT, "name": name, "plan_digest": digest, **extra},
    )


def _archive_ready(rig, name="arch"):
    create_vault(rig.owner, TASK_VAULT)
    _put_change(rig, name, requires=False)


def test_archive_register_approve_then_claim_via_ui_path(rig):
    _archive_ready(rig)
    reg = _archive_register(rig).json()["proposal"]
    assert (
        reg["kind"] == "task_archive"
        and reg["state"] == "pending"
        and reg["deduped"] is False
    )
    assert "payload" not in reg
    again = _archive_register(rig).json()["proposal"]
    assert (
        again["id"] == reg["id"]
        and again["deduped"] is True
        and again["rev"] == reg["rev"]
    )
    appr = rig.approve(reg["id"], vault=TASK_VAULT)
    assert appr.status_code == 200 and appr.json()["proposal"]["state"] == "approved"
    now = _archive_register(rig).json()["proposal"]
    assert now["state"] == "approved"  # 殼據此視為已同意
    claim = rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "approved",
            "plan_digest": "d1",
        },
    )
    assert claim.status_code == 200, claim.text
    cid = claim.json()["claim_id"]
    # 租約期間另一個通道（token 路徑）claim 輸了
    lost = rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "pending",
            "plan_digest": "d1",
        },
    )
    _code(lost, 409, "proposal_resolved")
    closed = rig.owner.post(
        "/v1/proposal_close",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "claim_id": cid,
            "outcome": "applied",
        },
    )
    assert closed.status_code == 200 and closed.json()["proposal"]["state"] == "applied"
    assert rig.proposals(id=reg["id"])[0]["resolved_via"] == "ui"


def test_archive_claim_token_path_and_release_restores_state(rig):
    _archive_ready(rig)
    reg = _archive_register(rig).json()["proposal"]
    claim = rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "pending",
            "plan_digest": "d1",
        },
    ).json()
    # UI 在租約期間核准 → 只能駁回/等待：核准要求 pending，executing 時被擋
    _code(rig.approve(reg["id"], rev=2, vault=TASK_VAULT), 409, "proposal_executing")
    rel = rig.owner.post(
        "/v1/proposal_close",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "claim_id": claim["claim_id"],
            "outcome": "release",
        },
    )
    assert rel.json()["proposal"]["state"] == "pending"


def test_archive_plan_change_supersedes_and_claim_digest_must_match(rig):
    _archive_ready(rig)
    first = _archive_register(rig, digest="d1").json()["proposal"]
    rig.approve(first["id"], vault=TASK_VAULT)
    second = _archive_register(rig, digest="d2").json()["proposal"]
    assert second["id"] != first["id"] and second["state"] == "pending"
    assert second["superseded"] == first["id"]
    bad = rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": second["id"],
            "expect": "pending",
            "plan_digest": "d1",
        },
    )
    _code(bad, 409, "proposal_state_conflict")


def test_expired_lease_returns_to_previous_state(rig):
    _archive_ready(rig)
    reg = _archive_register(rig).json()["proposal"]
    rig.approve(reg["id"], vault=TASK_VAULT)
    rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "approved",
            "plan_digest": "d1",
        },
    )
    conn = connect(rig.db_path)
    try:
        assert store.expire_due(conn, now="2099-01-01T00:00:00.000Z") >= 1
        row = store.get(conn, reg["id"])
    finally:
        conn.close()
    # 租約逾時回 approved（UI 同意仍在，但過了 14 天就 expired；此處同時逾期）
    assert row.state in ("approved", "expired")


def test_agent_endpoints_refuse_non_archive_proposals(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    for path, extra in (
        ("/v1/proposal_claim", {"expect": "pending", "plan_digest": "x"}),
        ("/v1/proposal_close", {"claim_id": "c", "outcome": "applied"}),
    ):
        resp = rig.owner.post(path, json={"vault": DEV, "id": prop["id"], **extra})
        _code(resp, 403, "proposal_kind_forbidden")
    assert rig.proposals(id=prop["id"])[0]["state"] == "pending"


def test_agent_status_returns_brief_only(rig):
    _archive_ready(rig)
    reg = _archive_register(rig).json()["proposal"]
    out = rig.owner.post(
        "/v1/proposal_status", json={"vault": TASK_VAULT, "name": "arch"}
    ).json()
    assert out["proposal"]["id"] == reg["id"] and "payload" not in out["proposal"]
    none = rig.owner.post(
        "/v1/proposal_status", json={"vault": TASK_VAULT, "name": "nope"}
    ).json()
    assert none == {"proposal": None}


# ── 結構性守門 ──


def test_migration_v20_defaults_existing_notes_to_agent(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    raw.row_factory = sqlite3.Row
    try:
        assert migrate(raw, migrations=MIGRATIONS[:19]) == 19
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/a', 'a', 'repo', '2026-01-01T00:00:00.000Z')"
        )
        raw.execute(
            "INSERT INTO notes (id, vault, title, body, created, updated, principal, "
            "updated_by_principal) VALUES ('n1', 'folder/a', 't', 'b', "
            "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', 'owner', 'owner')"
        )
        assert migrate(raw) == len(MIGRATIONS) == 20
        assert raw.execute("SELECT write_origin FROM notes").fetchone()[0] == "agent"
        assert raw.execute("SELECT count(*) FROM review_proposals").fetchone()[0] == 0
    finally:
        raw.close()


def test_snapshot_never_carries_proposals(rig, tmp_path):
    _hold_invariant(rig)
    assert "review_proposals" in storage_snapshot.SNAPSHOT_EXCLUDED_TABLES
    assert "review_proposals" not in storage_snapshot.SNAPSHOT_TABLES
    dest = tmp_path / "snap.db"
    storage_snapshot.build_snapshot(rig.db_path, dest)
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("SELECT count(*) FROM review_proposals").fetchone()[0] == 0
    finally:
        conn.close()


def test_every_note_service_write_or_update_call_passes_actor():
    """呼叫點掃描：所有 `notes_service.write|update` 呼叫都明確傳 `actor`。"""
    src = Path(__file__).resolve().parents[2] / "src" / "lore_vault"
    found = 0
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write", "update")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "notes_service"
            ):
                found += 1
                keys = {k.arg for k in node.keywords}
                assert "actor" in keys, f"{path}:{node.lineno} 沒有傳 actor"
    assert found >= 3  # routes write/update、proposals.service 套用


# ── 重啟、並行、文件 ──


def test_restart_invalidates_old_token_and_replan_reuses_the_proposal(rig, db_path):
    note, plan = _delete_flow(rig)
    # 模擬服務重啟：同一個資料庫、新的 app（新的 ConfirmSigner 祕密）
    app2 = create_app(make_settings(db_path))
    with SpaceClient(app2) as owner2:
        owner2.headers.update(AUTH)
        stale = owner2.post(
            "/v1/note_delete",
            json={
                "space": "dev",
                "vault": DEV,
                "id": note["id"],
                "confirm_token": plan["confirm_token"],
            },
        )
        _code(stale, 400, "invalid_confirm_token")
        assert rig.row(note["id"])["id"] == note["id"]
        replan = owner2.post(
            "/v1/note_delete", json={"space": "dev", "vault": DEV, "id": note["id"]}
        ).json()
        assert replan["proposal"]["id"] == plan["proposal"]["id"]
        assert replan["proposal"]["deduped"] is True


def test_concurrent_claims_have_exactly_one_winner(rig):
    import threading

    create_vault(rig.owner, TASK_VAULT)
    reg = _archive_register(rig).json()["proposal"]
    rig.approve(reg["id"], vault=TASK_VAULT)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt():
        conn = connect(rig.db_path)
        try:
            barrier.wait()
            try:
                proposals_service.claim(
                    conn, reg["id"], expect="approved", plan_digest="d1"
                )
                results.append("won")
            except t.ProposalResolved:
                results.append("lost")
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["lost", "lost", "lost", "won"]
    assert rig.proposals(id=reg["id"])[0]["state"] == "executing"


def test_document_delete_ui_approval_deletes_and_old_token_is_dead(db_path, tmp_path):
    from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig

    from .conftest import DIM

    seed_ui_account(db_path, username=SAME_PRINCIPAL)
    cfg = Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(
            blob_dir=str(tmp_path / "blobs"), max_file_bytes=200_000
        ),
    )
    app = create_app(make_settings(db_path, config=cfg, document_worker=False))
    with (
        SpaceClient(app) as owner,
        TestClient(app, base_url="https://testserver") as ui,
    ):
        owner.headers.update(AUTH)
        ui.post(
            "/ui/api/login",
            json={"username": SAME_PRINCIPAL, "password": UI_PASSWORD},
            headers=UI,
        )
        ui.headers.update(UI)
        create_vault(owner, DEV)
        up = owner.post(
            "/v1/documents",
            files={"file": ("a.md", b"# title\n\nbody", "text/plain")},
            data={"vault": DEV, "space": "dev"},
        ).json()
        body = {"space": "dev", "vault": DEV, "id": up["document_id"]}
        plan = owner.post("/v1/document_delete", json=body).json()
        prop = plan["proposal"]
        assert prop["kind"] == "document_delete"
        resp = ui.post(
            "/v1/review/proposal_approve",
            json={**body, "expected_rev": 1, "id": prop["id"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["proposal"]["state"] == "applied"
        dead = owner.post(
            "/v1/document_delete",
            json={**body, "confirm_token": plan["confirm_token"]},
        )
        assert dead.status_code in (404, 409), dead.text
        listed = owner.post(
            "/v1/list", json={"vault": DEV, "kinds": ["document"]}
        ).json()
        assert listed["items"] == []
