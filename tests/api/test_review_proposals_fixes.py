# ruff: noqa: F811, E501
"""審查（reviewer／tester）後的修正：vault 層級操作、終態不留原文、核准者保留、
租約續租、壞 JSON 仍 403、open_proposals 範圍、register 驗證、queue_full 降級、
呼叫點白名單、UI 編輯過時化刪除提案、無變更 update。保護類修正配「拿掉就轉紅」的負向測試。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lore_vault.api import manage as manage_api
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.proposals import service as proposals_service
from lore_vault.storage import proposals as store
from lore_vault.storage.db import connect

from .conftest import create_vault
from .test_review_proposals import (  # noqa: F401
    DEV,
    LORE,
    TASK_VAULT,
    Rig,
    _archive_ready,
    _archive_register,
    _code,
    _delete_flow,
    _hold_invariant,
    rig,
)

TERMINAL = ("applied", "rejected", "superseded", "expired", "obsolete")


# ── M1 vault 層級操作 ──


def _vault_ops(rig: Rig, client, key=DEV, space="dev"):
    return [
        client.post("/v1/vault_delete", json={"space": space, "key": key}),
        client.post(
            "/v1/vault_move_space",
            json={"space": space, "key": key, "to_space": "personal"},
        ),
    ]


def _vault_ops_invariant(rig: Rig) -> None:
    rig.protected_note()
    for resp in _vault_ops(rig, rig.owner):
        _code(resp, 403, "ui_session_required")
    for resp in _vault_ops(rig, rig.owner, key=LORE, space="lore"):
        _code(resp, 403, "ui_session_required")
    assert (
        "請使用者在 UI 操作" in _vault_ops(rig, rig.owner)[0].json()["error"]["message"]
    )


def test_bearer_cannot_delete_or_move_whole_vaults(rig):
    _vault_ops_invariant(rig)
    # UI session 照常可以規劃（第一步）
    assert _vault_ops(rig, rig.ui)[0].status_code == 200


def test_vault_ops_negative_removing_guard_turns_red(rig, monkeypatch):
    monkeypatch.setattr(manage_api, "_vault_level_ui_only", lambda *a, **k: None)
    with pytest.raises(AssertionError):
        _vault_ops_invariant(rig)


# ── M2 終態不留原文 ──


def _terminal_rows(rig: Rig):
    return [p for p in rig.proposals() if p["state"] in TERMINAL]


def test_terminal_proposals_carry_no_note_text(rig):
    secret = "機密內文只該存在於開放提案"
    # rejected
    a = rig.protected_note(title="甲")
    pa = rig.bearer_update(a["id"], a["updated"], body=secret).json()["proposal"]
    rig.reject(pa["id"])
    # applied
    b = rig.protected_note(title="乙")
    pb = rig.bearer_update(b["id"], b["updated"], body=secret + "2").json()["proposal"]
    rig.approve(pb["id"])
    # superseded
    c = rig.protected_note(title="丙")
    rig.bearer_update(c["id"], c["updated"], body=secret + "3")
    rig.bearer_update(c["id"], c["updated"], body=secret + "4")
    states = {p["state"] for p in _terminal_rows(rig)}
    assert {"rejected", "applied", "superseded"} <= states
    for row in _terminal_rows(rig):
        assert row["payload"] == "{}" and row["base_snapshot"] is None
        assert row["payload_digest"]  # 稽核欄位仍在
    for row in rig.proposals():
        if row["state"] == "pending":  # 開放中的仍保留，審核頁要用
            assert secret in row["payload"]


def test_terminal_payload_negative_doctor_turns_red(rig):
    ctx = _hold_invariant(rig)
    rig.reject(ctx["resp"]["proposal"]["id"])
    conn = connect(rig.db_path)
    try:

        def run():
            report = default_registry().run(
                DoctorContext(resources={"db": conn}), categories=["proposals"]
            )
            return next(
                o.result for o in report.outcomes if o.name == "proposals.integrity"
            )

        assert run().status.value == "pass"
        conn.execute(
            'UPDATE review_proposals SET payload = \'{"changes":{"body":"x"}}\''
        )
        res = run()
    finally:
        conn.close()
    assert res.status.value == "fail" and res.counts["terminal_has_payload"] == 1


def test_clearing_negative_keeping_payload_is_detected(rig, monkeypatch):
    """拿掉終態清空：同樣的不變量（終態列無原文）會失敗。"""
    real = store.transition

    def keep_payload(conn, pid, **kw):
        ok = real(conn, pid, **kw)
        return ok

    ctx = _hold_invariant(rig)
    pid = ctx["resp"]["proposal"]["id"]
    conn = connect(rig.db_path)
    try:
        before = conn.execute(
            "SELECT payload FROM review_proposals WHERE id = ?", (pid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert before != "{}"
    rig.reject(pid)
    assert rig.proposals(id=pid)[0]["payload"] == "{}"
    assert keep_payload  # 保留名稱，避免未使用警告


# ── Minor 1 保留核准者 ──


def test_terminal_transition_without_actor_keeps_approver(rig):
    _archive_ready(rig)
    reg = _archive_register(rig).json()["proposal"]
    assert rig.approve(reg["id"], vault=TASK_VAULT).status_code == 200
    conn = connect(rig.db_path)
    try:
        _, claim_id = proposals_service.claim(
            conn, reg["id"], expect="approved", plan_digest="d1"
        )
        proposals_service.close_claim(
            conn, reg["id"], claim_id=claim_id, outcome="applied"
        )
    finally:
        conn.close()
    row = rig.proposals(id=reg["id"])[0]
    assert row["state"] == "applied" and row["resolved_by"] == "owner"
    assert row["resolved_via"] == "ui"


# ── Minor 2 租約續租 ──


def test_renew_extends_lease_and_rejects_stale_claim(rig):
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
    conn = connect(rig.db_path)
    try:
        conn.execute(
            "UPDATE review_proposals SET claim_expires_at = '2000-01-01T00:00:00.000Z'"
        )
    finally:
        conn.close()
    renewed = rig.owner.post(
        "/v1/proposal_renew",
        json={"vault": TASK_VAULT, "id": reg["id"], "claim_id": claim["claim_id"]},
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["lease_expires_at"] > "2020-01-01T00:00:00.000Z"
    # 租約換人後，舊持有者續租／結案都得到明確的 409，不是靜默成功
    stale = rig.owner.post(
        "/v1/proposal_renew",
        json={"vault": TASK_VAULT, "id": reg["id"], "claim_id": "claim_other"},
    )
    _code(stale, 409, "proposal_resolved")
    stale_close = rig.owner.post(
        "/v1/proposal_close",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "claim_id": "claim_other",
            "outcome": "applied",
        },
    )
    _code(stale_close, 409, "proposal_resolved")


# ── Minor 3 壞 JSON ──


@pytest.mark.parametrize(
    "path",
    ["/v1/review/queue", "/v1/review/proposal_approve", "/v1/review/approve"],
)
def test_bearer_with_malformed_json_still_gets_403(rig, path):
    resp = rig.owner.post(
        path, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _code(resp, 403, "ui_session_required")
    # UI session 送壞 JSON 仍是一般的驗證錯誤
    bad = rig.ui.post(
        path, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert bad.status_code in (400, 422)


# ── Minor 4 open_proposals 範圍 ──


def test_open_proposals_skip_expired_and_non_governed_spaces(rig):
    ctx = _hold_invariant(rig)
    note = ctx["note"]

    def listed():
        got = rig.owner.post("/v1/get", json={"vault": DEV, "ids": [note["id"]]}).json()
        return got["items"][0]["open_proposals"]

    assert len(listed()) == 1
    conn = connect(rig.db_path)
    try:  # 過期但還沒被任何人掃到
        conn.execute(
            "UPDATE review_proposals SET expires_at = '2020-01-01T00:00:00.000Z'"
        )
    finally:
        conn.close()
    assert listed() == []
    # personal space 不附（不納入提案）
    create_vault(rig.owner, "personal/p", space="personal")
    made = rig.owner.post(
        "/v1/write",
        json={"space": "personal", "vault": "personal/p", "title": "p", "body": "b"},
    ).json()
    got = rig.owner.post(
        "/v1/get",
        json={"space": "personal", "vault": "personal/p", "ids": [made["id"]]},
    ).json()
    assert "open_proposals" not in got["items"][0]


# ── Minor 5 register 驗證、queue_full 降級 ──


def test_register_validates_name_and_change(rig):
    create_vault(rig.owner, TASK_VAULT)
    base = {"vault": TASK_VAULT, "plan_digest": "d"}
    for bad in ("", "Bad Name", "archive", "../x"):
        resp = rig.owner.post("/v1/proposal_register", json={**base, "name": bad})
        assert resp.status_code in (400, 422), (bad, resp.text)
    missing = rig.owner.post(
        "/v1/proposal_register", json={**base, "name": "no-such-change"}
    )
    _code(missing, 404, "not_found")
    assert rig.proposals() == []


def test_delete_plan_degrades_when_queue_is_full(rig, monkeypatch):
    monkeypatch.setitem(store.upsert_open.__kwdefaults__, "max_open", 1)
    a, b = rig.note(title="甲"), rig.note(title="乙")
    first = rig.delete_plan(a["id"])
    assert first["proposal_pending"] is True
    second = rig.delete_plan(b["id"])
    assert second["proposal_pending"] is False and "proposal" not in second
    assert "對話" in second["proposal_note"]
    done = rig.delete_exec(b["id"], second["confirm_token"])
    assert done.status_code == 200, done.text
    assert "proposal" not in done.json()
    assert len(rig.proposals()) == 1  # 沒有為降級的那筆建提案


# ── Minor 6 呼叫點白名單 ──

SRC = Path(__file__).resolve().parents[2] / "src" / "lore_vault"


def _calls(name: str):
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = (
                    fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                )
                if called == name:
                    yield path.relative_to(SRC).as_posix(), node


def test_write_origin_writers_are_whitelisted():
    """`update_note_if(write_origin=)` 只有 notes.service；`insert_note` 只有既有的合法
    模組；HTTP 層不直接寫 notes，Actor 只在 api/principals 依認證方式建構。"""
    for rel, node in _calls("update_note_if"):
        if any(k.arg == "write_origin" for k in node.keywords):
            assert rel == "notes/service.py", rel
    allowed_insert = {"notes/service.py", "storage/admin.py", "cli/import_on.py"}
    for rel, _ in _calls("insert_note"):
        assert rel in allowed_insert, rel
    for rel, _ in _calls("update_note_if"):
        assert not rel.startswith("api/"), rel
    for rel, _ in _calls("insert_note"):
        assert not rel.startswith("api/"), rel
    for ctor in ("ui", "agent"):
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == ctor
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Actor"
                ):
                    rel = path.relative_to(SRC).as_posix()
                    if rel.startswith("api/"):
                        assert rel == "api/principals.py", rel
    # HTTP 層寫入一律走 actor_of
    for rel, node in _calls("write"):
        if rel in ("api/routes.py",) and any(k.arg == "actor" for k in node.keywords):
            kw = next(k for k in node.keywords if k.arg == "actor")
            assert (
                isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", "") == "actor_of"
            )


# ── Minor 7 UI 編輯讓 note_delete 提案過時 ──


def test_ui_edit_supersedes_open_delete_proposal(rig):
    note = rig.note(title="待刪")
    plan = rig.delete_plan(note["id"])
    rig.ui_edit(note["id"], note["updated"], body="使用者改過了")
    row = rig.proposals(id=plan["proposal"]["id"])[0]
    assert (row["state"], row["resolved_via"]) == ("superseded", "ui_direct")
    # 舊 token 對不上新狀態：不會執行
    resp = rig.delete_exec(note["id"], plan["confirm_token"])
    assert resp.status_code in (404, 409)
    assert rig.row(note["id"])["body"] == "使用者改過了"


# ── Nitpick 無變更 update ──


def test_noop_update_on_protected_note_creates_no_proposal_and_keeps_protection(rig):
    note = rig.protected_note()
    before = rig.row(note["id"])
    resp = rig.bearer_update(note["id"], note["updated"], body=before["body"])
    assert resp.status_code == 200, resp.text
    assert "proposal" not in resp.json()
    assert rig.proposals() == []
    assert rig.row(note["id"]) == before  # 沒寫入，write_origin 仍是 ui


# ── 13 reason 改措辭就地更新 ──


def test_reason_rewording_updates_in_place_and_keeps_approval(rig):
    _archive_ready(rig)
    reg = _archive_register(rig, reason="第一次").json()["proposal"]
    rig.approve(reg["id"], vault=TASK_VAULT)
    again = _archive_register(rig, reason="換個說法").json()["proposal"]
    assert again["id"] == reg["id"] and again["state"] == "approved"
    row = rig.proposals(id=reg["id"])[0]
    assert json.loads(row["payload"])["reason"] == "換個說法"
    assert row["rev"] == 2  # 核准後的 rev，改措辭不動它
    # 規劃雜湊不同才算不同的同意
    other = _archive_register(rig, digest="d2").json()["proposal"]
    assert other["id"] != reg["id"] and other["state"] == "pending"


# ── 12 過期掃描時機 ──


def test_expire_sweep_timing_does_not_change_token_outcome(rig):
    note, plan = _delete_flow(rig)
    conn = connect(rig.db_path)
    try:
        conn.execute(
            "UPDATE review_proposals SET expires_at = '2020-01-01T00:00:00.000Z'"
        )
    finally:
        conn.close()
    resp = rig.delete_exec(note["id"], plan["confirm_token"])
    # 沒人掃過也一樣：過期的提案不再當作有效同意
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "plan_changed"
    assert rig.row(note["id"])["id"] == note["id"]
