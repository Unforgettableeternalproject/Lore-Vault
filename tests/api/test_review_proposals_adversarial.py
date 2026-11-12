# ruff: noqa: F811, B023, B905, E501
"""待審提案的破壞性驗證（add-agent-review-proposals 7.1）：跨通道競態、去重／上限、
基準衝突、write_origin 偽造、連結邊界、範圍隔離，以及「拿掉保護就轉紅」的負向測試。

只補 test_review_proposals.py 沒蓋到的情境；沿用同一個 rig（獨立 app／資料庫／簽章祕密）。
競態用執行緒＋Barrier 製造交錯；結果斷言「只有一方成功、輸家拿到明確錯誤碼、
資料庫終態一致」，不依賴誰先到。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import guards
from lore_vault.api import principals as principals_mod
from lore_vault.api.app import create_app
from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.storage import notes as storage_notes
from lore_vault.storage import proposals as store
from lore_vault.storage.db import connect
from lore_vault.storage.migrate import MIGRATIONS, migrate

from .conftest import AUTH, SpaceClient, create_vault, embed_all, make_settings
from .test_review_proposals import (  # noqa: F401  (rig 是 fixture，需要匯入才會被收集)
    DEV,
    DEV2,
    LORE,
    SAME_PRINCIPAL,
    TASK_VAULT,
    UEP_TOKEN,
    UI,
    Rig,
    _archive_ready,
    _archive_register,
    _code,
    _delete_flow,
    _hold_invariant,
    rig,
)

PAST = "2020-01-01T00:00:00.000Z"
# 競態可接受的「輸家」錯誤碼（必須是明確的，不能是 5xx 或例外）
LOSER_CODES = {
    "not_found",
    "proposal_state_conflict",
    "proposal_resolved",
    "proposal_executing",
    "plan_changed",
    "version_conflict",
}


# ── 共用輔助 ──


def _parallel(*calls):
    """同時放行所有 callable；回傳各自的結果或例外（不讓例外吞掉）。"""
    barrier = threading.Barrier(len(calls))
    out: list = [None] * len(calls)

    def wrap(i, fn):
        barrier.wait(timeout=20)
        try:
            out[i] = fn()
        except BaseException as exc:  # noqa: BLE001
            out[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, c)) for i, c in enumerate(calls)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert all(not th.is_alive() for th in threads), "執行緒卡住（可能死鎖）"
    return out


def _err(resp) -> str | None:
    if isinstance(resp, BaseException):
        return None
    try:
        return resp.json().get("error", {}).get("code")
    except ValueError:
        return None


@contextmanager
def _bearers(rig: Rig, count: int):
    """多個獨立的 bearer client（各自的 portal），供執行緒使用。"""
    clients = [SpaceClient(rig.ui.app) for _ in range(count)]
    for c in clients:
        c.__enter__()
        c.headers.update(AUTH)
    try:
        yield clients
    finally:
        for c in clients:
            c.__exit__(None, None, None)


def _sql(rig: Rig, sql: str, *args) -> None:
    conn = connect(rig.db_path)
    try:
        conn.execute(sql, args)
    finally:
        conn.close()


def _expire_all(rig: Rig) -> None:
    _sql(rig, "UPDATE review_proposals SET expires_at = ?", PAST)


def _external_update(rig: Rig, note_id: str, fields: dict, vault: str = DEV):
    """繞過 HTTP 的非 UI 寫入（例如 CLI／匯入），不改 write_origin。"""
    conn = connect(rig.db_path)
    try:
        return storage_notes.update_note_if(
            conn,
            vault,
            note_id,
            rig.row(note_id)["updated"],
            fields,
            space="dev",
            editor=(None, "someone"),
        )
    finally:
        conn.close()


def _get(rig: Rig, note_id: str, vault: str = DEV) -> dict:
    return rig.owner.post("/v1/get", json={"vault": vault, "ids": [note_id]}).json()[
        "items"
    ][0]


# ── 1. 跨通道競態 ──


def test_race_ui_approve_vs_token_execute_exactly_one_executes(rig):
    """UI 核准刪除與 agent 帶 token 執行第二步同時發生：只執行一次，輸家有明確錯誤碼。"""
    wins = {"ui": 0, "token": 0}
    for _ in range(10):
        note, plan = _delete_flow(rig)
        prop_id = plan["proposal"]["id"]
        with _bearers(rig, 1) as (bearer,):
            ui_resp, tok_resp = _parallel(
                lambda: rig.ui.post(
                    "/v1/review/proposal_approve",
                    json={
                        "space": "dev",
                        "vault": DEV,
                        "id": prop_id,
                        "expected_rev": 1,
                    },
                ),
                lambda: bearer.post(
                    "/v1/note_delete",
                    json={
                        "space": "dev",
                        "vault": DEV,
                        "id": note["id"],
                        "confirm_token": plan["confirm_token"],
                    },
                ),
            )
        for r in (ui_resp, tok_resp):
            assert not isinstance(r, BaseException), repr(r)
            assert r.status_code < 500, r.text
        oks = [r for r in (ui_resp, tok_resp) if r.status_code == 200]
        assert len(oks) == 1, (ui_resp.text, tok_resp.text)
        loser = tok_resp if oks[0] is ui_resp else ui_resp
        assert _err(loser) in LOSER_CODES, loser.text
        final = rig.proposals(id=prop_id)[0]
        assert final["state"] == "applied"
        assert final["resolved_via"] == ("ui" if oks[0] is ui_resp else "token")
        conn = connect(rig.db_path)
        try:  # note 確實只被刪一次
            assert not conn.execute(
                "SELECT 1 FROM notes WHERE id = ?", (note["id"],)
            ).fetchone()
        finally:
            conn.close()
        wins["ui" if oks[0] is ui_resp else "token"] += 1
        assert rig.proposals(id=prop_id)[0]["rev"] == 2  # 只轉移一次
    assert sum(wins.values()) == 10


def test_race_many_plans_of_same_target_converge_on_one_proposal(rig):
    note = rig.note(title="多方規劃")
    with _bearers(rig, 6) as clients:
        resps = _parallel(
            *[
                (
                    lambda c=c: c.post(
                        "/v1/note_delete",
                        json={"space": "dev", "vault": DEV, "id": note["id"]},
                    )
                )
                for c in clients
            ]
        )
    for r in resps:
        assert not isinstance(r, BaseException), repr(r)
        assert r.status_code == 200, r.text
    ids = {r.json()["proposal"]["id"] for r in resps}
    assert len(ids) == 1
    assert sum(1 for r in resps if not r.json()["proposal"]["deduped"]) == 1
    assert len(rig.proposals(kind="note_delete")) == 1


def test_race_conflicting_updates_leave_exactly_one_open_proposal(rig):
    note = rig.protected_note()
    with _bearers(rig, 6) as clients:
        resps = _parallel(
            *[
                (
                    lambda c=c, i=i: c.post(
                        "/v1/update",
                        json={
                            "vault": DEV,
                            "id": note["id"],
                            "expected_updated": note["updated"],
                            "body": f"版本 {i}",
                        },
                    )
                )
                for i, c in enumerate(clients)
            ]
        )
    for r in resps:
        assert not isinstance(r, BaseException), repr(r)
        assert r.status_code == 202, r.text
    rows = rig.proposals(kind="note_update")
    assert len(rows) == 6
    assert [r["state"] for r in rows].count("pending") == 1
    assert [r["state"] for r in rows].count("superseded") == 5
    # superseded 鏈沒有分叉：每個 superseded 指向一個存在的提案，且沒有兩個指向同一個
    targets = [r["superseded_by"] for r in rows if r["state"] == "superseded"]
    assert len(set(targets)) == 5 and set(targets) <= {r["id"] for r in rows}
    assert rig.row(note["id"])["body"] == "人類改過的內文"


def test_race_ui_approve_vs_reject_exactly_one_wins(rig):
    for _ in range(6):
        ctx = _hold_invariant_fresh(rig)
        prop = ctx["resp"]["proposal"]
        body = {"space": "dev", "vault": DEV, "id": prop["id"], "expected_rev": 1}
        with TestClient(rig.ui.app, base_url="https://testserver") as ui2:
            assert (
                ui2.post(
                    "/ui/api/login",
                    json={"username": SAME_PRINCIPAL, "password": _ui_password()},
                    headers=UI,
                ).status_code
                == 204
            )
            ui2.headers.update(UI)
            appr, rej = _parallel(
                lambda: rig.ui.post("/v1/review/proposal_approve", json=body),
                lambda: ui2.post(
                    "/v1/review/proposal_reject", json={**body, "reason": "不要"}
                ),
            )
        for r in (appr, rej):
            assert not isinstance(r, BaseException), repr(r)
            assert r.status_code < 500, r.text
        assert sorted([appr.status_code, rej.status_code]) == [200, 409]
        loser = rej if appr.status_code == 200 else appr
        assert _err(loser) in LOSER_CODES
        final = rig.proposals(id=prop["id"])[0]
        row = rig.row(ctx["note"]["id"])
        if appr.status_code == 200:
            assert (final["state"], row["body"]) == ("applied", "agent 想改的內文")
        else:  # 駁回贏：note 整列沒動
            assert (final["state"], row["body"]) == ("rejected", "人類改過的內文")
            assert row == ctx["before"]


def test_race_ui_approve_vs_agent_resubmit(rig):
    """核准的同時 agent 以不同內容重新提出：不能出現「P1 已套用、P2 卻以舊基準 pending」。"""
    for i in range(6):
        ctx = _hold_invariant_fresh(rig)
        note, p1 = ctx["note"], ctx["resp"]["proposal"]
        with _bearers(rig, 1) as (bearer,):
            appr, resub = _parallel(
                lambda: rig.ui.post(
                    "/v1/review/proposal_approve",
                    json={
                        "space": "dev",
                        "vault": DEV,
                        "id": p1["id"],
                        "expected_rev": 1,
                    },
                ),
                lambda: bearer.post(
                    "/v1/update",
                    json={
                        "vault": DEV,
                        "id": note["id"],
                        "expected_updated": note["updated"],
                        "body": f"重新提出 {i}",
                    },
                ),
            )
        for r in (appr, resub):
            assert not isinstance(r, BaseException), repr(r)
            assert r.status_code < 500, r.text
        first = rig.proposals(id=p1["id"])[0]
        pend = [
            p
            for p in rig.proposals(kind="note_update", target_id=note["id"])
            if p["state"] == "pending"
        ]
        if first["state"] == "applied":
            # 核准先到：agent 的舊基準更新必須被樂觀鎖擋下，不能留下 pending
            assert appr.status_code == 200
            assert resub.status_code == 409 and _err(resub) == "version_conflict"
            assert pend == []
        else:
            # agent 先到：P1 被取代，核准輸
            assert first["state"] == "superseded"
            assert appr.status_code == 409 and _err(appr) in LOSER_CODES
            assert len(pend) == 1
            assert rig.row(note["id"])["body"] == "人類改過的內文"


def test_race_ui_approve_archive_vs_shell_claim(rig):
    """task_archive：UI 核准（pending→approved）與殼憑 token claim(pending) 同時到達。"""
    create_vault(rig.owner, TASK_VAULT)
    for i in range(6):
        name = f"arch{i}"
        reg = _archive_register(rig, name=name).json()["proposal"]
        with _bearers(rig, 1) as (shell,):
            appr, claim = _parallel(
                lambda: rig.ui.post(
                    "/v1/review/proposal_approve",
                    json={
                        "space": "dev",
                        "vault": TASK_VAULT,
                        "id": reg["id"],
                        "expected_rev": 1,
                    },
                ),
                lambda: shell.post(
                    "/v1/proposal_claim",
                    json={
                        "vault": TASK_VAULT,
                        "id": reg["id"],
                        "expect": "pending",
                        "plan_digest": "d1",
                    },
                ),
            )
        for r in (appr, claim):
            assert not isinstance(r, BaseException), repr(r)
            assert r.status_code < 500, r.text
        state = rig.proposals(id=reg["id"])[0]["state"]
        if state == "executing":  # 殼贏：核准要拿到明確的 executing 錯誤
            assert claim.status_code == 200
            assert appr.status_code == 409 and _err(appr) == "proposal_executing"
        else:  # 核准贏：殼 claim(pending) 輸了
            assert state == "approved" and appr.status_code == 200
            assert claim.status_code == 409 and _err(claim) == "proposal_resolved"


def test_race_archive_ui_claim_vs_token_claim_one_lease(rig):
    create_vault(rig.owner, TASK_VAULT)
    reg = _archive_register(rig).json()["proposal"]
    rig.approve(reg["id"], vault=TASK_VAULT)
    with _bearers(rig, 4) as shells:
        resps = _parallel(
            *[
                (
                    lambda c=c, e=e: c.post(
                        "/v1/proposal_claim",
                        json={
                            "vault": TASK_VAULT,
                            "id": reg["id"],
                            "expect": e,
                            "plan_digest": "d1",
                        },
                    )
                )
                for c, e in zip(shells, ["approved", "pending", "approved", "pending"])
            ]
        )
    codes = sorted(r.status_code for r in resps)
    assert codes == [200, 409, 409, 409], [r.text for r in resps]
    assert all(_err(r) == "proposal_resolved" for r in resps if r.status_code == 409)
    row = rig.proposals(id=reg["id"])[0]
    assert row["state"] == "executing" and row["claim_id"]


# 競態輔助：每圈一則全新的受保護 note + 提案
def _hold_invariant_fresh(rig: Rig) -> dict:
    note = rig.protected_note()
    before = rig.row(note["id"])
    resp = rig.bearer_update(note["id"], note["updated"], body="agent 想改的內文")
    assert resp.status_code == 202, resp.text
    return {"note": note, "before": before, "resp": resp.json()}


def _ui_password() -> str:
    from .conftest import UI_PASSWORD

    return UI_PASSWORD


# ── 2. 去重、supersede、上限、過期 ──


def _fill_open(rig: Rig, count: int, vault: str = DEV) -> None:
    conn = connect(rig.db_path)
    try:
        for i in range(count):
            store.upsert_open(
                conn,
                space="dev",
                vault=vault,
                kind=t.KIND_NOTE_UPDATE,
                target_id=f"filler-{i}",
                payload={"changes": {"body": f"x{i}"}},
                proposer_principal="owner",
            )
    finally:
        conn.close()


def test_open_cap_is_200_per_vault_and_failed_update_changes_nothing(rig):
    assert t.MAX_OPEN_PER_VAULT == 200
    a = rig.protected_note(title="甲")
    first = rig.bearer_update(a["id"], a["updated"], body="第一版")
    assert first.status_code == 202
    _fill_open(rig, 199)  # 加上 a 的那一筆正好 200
    assert store.count_open(connect(rig.db_path), DEV) == 200
    b = rig.protected_note(title="乙")
    before = rig.row(b["id"])
    _code(
        rig.bearer_update(b["id"], b["updated"], body="被擋"),
        429,
        "proposal_queue_full",
    )
    assert rig.row(b["id"]) == before
    assert len(rig.proposals(target_id=b["id"])) == 0
    # 已存在目標的去重與取代不受上限影響（沒有淨增加）
    same = rig.bearer_update(a["id"], a["updated"], body="第一版")
    assert same.status_code == 202 and same.json()["proposal"]["deduped"] is True
    diff = rig.bearer_update(a["id"], a["updated"], body="第二版")
    assert diff.status_code == 202 and diff.json()["proposal"]["superseded"]
    assert store.count_open(connect(rig.db_path), DEV) == 200
    # 別的 vault 不受影響
    other = rig.note(title="別處", vault=DEV2)
    edited = rig.ui_edit(other["id"], other["updated"], vault=DEV2, body="人類改過")
    ok = rig.bearer_update(other["id"], edited["updated"], vault=DEV2, body="可以")
    assert ok.status_code == 202
    # 處理掉一筆後又能提出
    rig.reject(rig.proposals(target_id=a["id"], state="pending")[0]["id"], rev=1)
    assert rig.bearer_update(b["id"], b["updated"], body="現在可以").status_code == 202


def test_open_cap_blocks_delete_plan_and_partial_update_rolls_back(rig):
    a, b, c = (rig.note(title=t_) for t_ in ("甲", "乙", "丙"))
    linked = rig.note(title="連", links=[b["id"]])
    _fill_open(rig, 200)
    plan = rig.owner.post(
        "/v1/note_delete", json={"space": "dev", "vault": DEV, "id": a["id"]}
    )
    _code(plan, 429, "proposal_queue_full")
    # 部分套用：移除連結被攔、同時改標題。上限已滿 → 整筆 429，標題不能偷偷先寫入
    before = rig.row(linked["id"])
    _code(
        rig.bearer_update(linked["id"], linked["updated"], title="新標題", links=[]),
        429,
        "proposal_queue_full",
    )
    assert rig.row(linked["id"]) == before
    assert c["id"]


def test_expired_proposal_cannot_be_approved_or_rejected_or_claimed(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    create_vault(rig.owner, TASK_VAULT)
    reg = _archive_register(rig).json()["proposal"]
    _expire_all(rig)
    # 不先經過佇列（惰性過期）：核准／駁回本身就要識別出過期
    _code(rig.approve(prop["id"]), 409, "proposal_state_conflict")
    _code(rig.reject(prop["id"]), 409, "proposal_state_conflict")
    assert rig.row(ctx["note"]["id"]) == ctx["before"]
    _code(rig.approve(reg["id"], vault=TASK_VAULT), 409, "proposal_state_conflict")
    claim = rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "pending",
            "plan_digest": "d1",
        },
    )
    _code(claim, 409, "proposal_resolved")
    # 過期後佇列不再列出，狀態確實轉 expired
    assert rig.queue()["items"] == []
    assert {p["state"] for p in rig.proposals()} == {"expired"}


def test_expired_proposal_is_not_resurrected_by_dedupe(rig):
    ctx = _hold_invariant(rig)
    first = ctx["resp"]["proposal"]
    _expire_all(rig)
    again = rig.bearer_update(
        ctx["note"]["id"], ctx["note"]["updated"], body="agent 想改的內文"
    ).json()["proposal"]
    assert again["id"] != first["id"] and again["deduped"] is False
    assert rig.proposals(id=first["id"])[0]["state"] == "expired"
    assert rig.approve(again["id"]).status_code == 200


def test_rejected_content_can_be_proposed_again_and_supersede_chain_is_linear(rig):
    ctx = _hold_invariant(rig)
    note = ctx["note"]
    p1 = ctx["resp"]["proposal"]
    assert rig.reject(p1["id"]).status_code == 200
    p2 = rig.bearer_update(note["id"], note["updated"], body="agent 想改的內文").json()[
        "proposal"
    ]
    assert p2["id"] != p1["id"] and p2["deduped"] is False
    p3 = rig.bearer_update(note["id"], note["updated"], body="第三個").json()[
        "proposal"
    ]
    p4 = rig.bearer_update(note["id"], note["updated"], body="第四個").json()[
        "proposal"
    ]
    chain = {p["id"]: p for p in rig.proposals(kind="note_update")}
    assert chain[p1["id"]]["state"] == "rejected"
    assert chain[p2["id"]]["superseded_by"] == p3["id"]
    assert chain[p3["id"]]["superseded_by"] == p4["id"]
    assert chain[p4["id"]]["state"] == "pending"
    # 被取代的舊提案不能再核准，核准最新的才生效
    _code(rig.approve(p2["id"]), 409, "proposal_state_conflict")
    _code(rig.approve(p3["id"]), 409, "proposal_state_conflict")
    assert rig.row(note["id"])["body"] == "人類改過的內文"
    assert rig.approve(p4["id"]).status_code == 200
    assert rig.row(note["id"])["body"] == "第四個"


def test_same_content_on_different_notes_is_not_deduped(rig):
    a, b = rig.protected_note(title="甲"), rig.protected_note(title="乙")
    pa = rig.bearer_update(a["id"], a["updated"], body="相同").json()["proposal"]
    pb = rig.bearer_update(b["id"], b["updated"], body="相同").json()["proposal"]
    assert pa["id"] != pb["id"] and pb["deduped"] is False


def test_archive_register_while_executing_cannot_replace_the_leased_proposal(rig):
    _archive_ready(rig)
    reg = _archive_register(rig, digest="d1").json()["proposal"]
    rig.owner.post(
        "/v1/proposal_claim",
        json={
            "vault": TASK_VAULT,
            "id": reg["id"],
            "expect": "pending",
            "plan_digest": "d1",
        },
    )
    _code(_archive_register(rig, digest="d2"), 409, "proposal_executing")
    assert rig.proposals(id=reg["id"])[0]["state"] == "executing"
    assert len(rig.proposals(kind="task_archive")) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "proposals/service.py register_archive 以整份 payload（含 reason）計算 digest："
        "plan_digest 相同、只有 agent 自填的 reason 措辭不同時，舊的 approved 提案被 "
        "superseded，使用者的核准被洗掉，需要重新核准。若認定 reason 屬於同意內容，"
        "請把此測試改成反向斷言。"
    ),
)
def test_archive_consent_survives_reason_rewording_with_same_plan_digest(rig):
    _archive_ready(rig)
    reg = _archive_register(rig, digest="d1", reason="第一次措辭").json()["proposal"]
    assert rig.approve(reg["id"], vault=TASK_VAULT).status_code == 200
    again = _archive_register(rig, digest="d1", reason="換個說法").json()["proposal"]
    assert again["id"] == reg["id"] and again["state"] == "approved"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "api/manage.py _two_phase 的 bind(upsert=False) 用 find_open 讀提案、沒有先 "
        "expire_due：過期但尚未被掃到的刪除提案，帶 token 第二步直接執行並結案為 applied；"
        "被佇列／upsert 掃過之後同一份 token 卻回 409 plan_changed。結果取決於有沒有人"
        "先開過審核頁。"
    ),
)
def test_expired_delete_proposal_token_outcome_does_not_depend_on_sweep_timing(rig):
    swept_note, swept_plan = _delete_flow(rig)
    lazy_note, lazy_plan = _delete_flow(rig)
    _expire_all(rig)
    rig.queue()  # 惰性掃描：兩個提案都轉 expired
    swept = rig.delete_exec(swept_note["id"], swept_plan["confirm_token"])
    # 另一筆：把提案狀態還原成「過期但尚未掃到」的樣子再執行
    _sql(
        rig,
        "UPDATE review_proposals SET state = 'pending', rev = 1 WHERE id = ?",
        lazy_plan["proposal"]["id"],
    )
    lazy = rig.delete_exec(lazy_note["id"], lazy_plan["confirm_token"])
    assert lazy.status_code == swept.status_code, (swept.text, lazy.text)


# ── 3. 基準衝突 ──


def test_stale_lists_every_touched_field_that_drifted_and_stays_pending(rig):
    note = rig.protected_note()
    resp = rig.bearer_update(
        note["id"], note["updated"], title="agent 標題", body="agent 內文"
    )
    prop = resp.json()["proposal"]
    _external_update(rig, note["id"], {"title": "別人改標題"})
    stale = rig.approve(prop["id"])
    _code(stale, 409, "proposal_stale")
    err = stale.json()["error"]
    assert err["fields"] == ["title"]  # body 沒變不列入
    assert err["current"]["title"] == "別人改標題" and err["base"]["title"] == "筆記"
    row = rig.proposals(id=prop["id"])[0]
    assert (row["state"], row["rev"]) == ("pending", 1)  # 失敗不改提案，仍可駁回
    assert rig.reject(prop["id"]).status_code == 200


def test_rebase_to_must_equal_current_version_and_only_touches_proposed_fields(rig):
    ctx = _hold_invariant(rig)
    note, prop = ctx["note"], ctx["resp"]["proposal"]
    first = _external_update(rig, note["id"], {"body": "別人改的"})
    second = _external_update(rig, note["id"], {"topics": ("t1",)})
    assert first.updated != second.updated
    # 帶過期的版本（第一次改完的 updated，現在已不是目前版本）→ 仍是 stale
    _code(rig.approve(prop["id"], rebase_to=first.updated), 409, "proposal_stale")
    _code(rig.approve(prop["id"], rebase_to="亂填"), 409, "proposal_stale")
    assert rig.row(note["id"])["body"] == "別人改的"
    ok = rig.approve(prop["id"], rebase_to=second.updated)
    assert ok.status_code == 200, ok.text
    row = rig.row(note["id"])
    assert row["body"] == "agent 想改的內文"
    assert json.loads(row["topics"]) == ["t1"]  # 未被觸及的欄位保留別人的修改
    assert row["write_origin"] == "ui"


def test_rebase_to_is_harmless_when_nothing_drifted(rig):
    ctx = _hold_invariant(rig)
    prop = ctx["resp"]["proposal"]
    assert rig.approve(prop["id"], rebase_to="任意值").status_code == 200
    assert rig.row(ctx["note"]["id"])["body"] == "agent 想改的內文"


def test_links_drift_makes_link_touching_proposal_stale(rig):
    b, c = rig.note(title="乙"), rig.note(title="丙")
    note = rig.protected_note()
    prop = rig.bearer_update(note["id"], note["updated"], links=[b["id"]]).json()[
        "proposal"
    ]
    _external_update(rig, note["id"], {"links": (c["id"],)})
    _code(rig.approve(prop["id"]), 409, "proposal_stale")
    assert json.loads(rig.row(note["id"])["links"]) == [c["id"]]


# ── 4. write_origin ──


@pytest.mark.parametrize(
    "extra_body, headers, query",
    [
        ({"write_origin": "ui"}, {}, ""),
        ({"origin": "ui", "actor": "ui", "kind": "ui"}, {}, ""),
        ({}, {"X-Lore-Vault-UI": "1"}, ""),
        ({}, {"X-Write-Origin": "ui", "X-Actor": "ui"}, ""),
        ({}, {}, "?write_origin=ui&actor=ui"),
    ],
)
def test_bearer_cannot_forge_write_origin_by_any_parameter(
    rig, extra_body, headers, query
):
    created = rig.owner.post(
        "/v1/write" + query,
        json={"vault": DEV, "title": "偽造寫", "body": "b", **extra_body},
        headers=headers,
    )
    if created.status_code == 201:
        assert rig.row(created.json()["id"])["write_origin"] == "agent"
        note_id, updated = created.json()["id"], created.json()["updated"]
    else:  # 多餘欄位被拒絕也可以，但絕不能因此變成 ui
        assert created.status_code in (400, 422), created.text
        base = rig.note()
        note_id, updated = base["id"], base["updated"]
    upd = rig.owner.post(
        "/v1/update" + query,
        json={
            "vault": DEV,
            "id": note_id,
            "expected_updated": updated,
            "body": "偽造改",
            **extra_body,
        },
        headers=headers,
    )
    assert upd.status_code in (200, 400, 422), upd.text
    assert rig.row(note_id)["write_origin"] == "agent"
    # 帶 UI header 的 bearer 仍不是 UI session：審核端點照樣擋
    resp = rig.owner.post(
        "/v1/review/queue", json={"space": "dev", "vault": "*"}, headers=headers
    )
    _code(resp, 403, "ui_session_required")


def test_bearer_cannot_win_protection_by_matching_ui_origin_via_update_then_write(rig):
    """agent 更新受保護 note 後，note 的來源與 updated_by 都不能被改成 ui／使用者。"""
    ctx = _hold_invariant(rig)
    row = rig.row(ctx["note"]["id"])
    assert row["write_origin"] == "ui"  # 使用者的保護沒被 agent 的嘗試動到
    for body in ("再試一次", "又一次"):
        resp = rig.bearer_update(ctx["note"]["id"], ctx["note"]["updated"], body=body)
        assert resp.status_code == 202
    assert rig.row(ctx["note"]["id"]) == ctx["before"]


def test_uep_writes_are_agent_origin_even_when_edited_by_ui_later(rig):
    uep = SpaceClient(rig.ui.app)
    uep.__enter__()
    try:
        uep.headers.update({"Authorization": f"Bearer {UEP_TOKEN}"})
        made = uep.post(
            "/v1/write", json={"vault": DEV, "title": "uep 寫", "body": "b"}
        ).json()
        assert rig.row(made["id"])["write_origin"] == "agent"
        edited = rig.ui_edit(made["id"], made["updated"], body="使用者改")
        assert rig.row(made["id"])["write_origin"] == "ui"
        # uep 在 dev 維持直接生效：即使 note 已是 ui 來源也不送審
        resp = uep.post(
            "/v1/update",
            json={
                "vault": DEV,
                "id": made["id"],
                "expected_updated": edited["updated"],
                "body": "uep 再改",
            },
        )
        assert resp.status_code == 200, resp.text
        assert "proposal" not in resp.json()
        assert rig.row(made["id"])["body"] == "uep 再改"
        assert rig.proposals() == []
    finally:
        uep.__exit__(None, None, None)


def test_embedding_backfill_does_not_change_write_origin(rig):
    human = rig.protected_note(title="人寫")
    agent = rig.note(title="agent 寫")
    embed_all(rig.db_path)
    assert rig.row(human["id"])["write_origin"] == "ui"
    assert rig.row(agent["id"])["write_origin"] == "agent"
    assert rig.row(human["id"])["updated"] == human["updated"]


def test_rejecting_and_expiring_keep_write_origin(rig):
    ctx = _hold_invariant(rig)
    rig.reject(ctx["resp"]["proposal"]["id"])
    assert rig.row(ctx["note"]["id"])["write_origin"] == "ui"
    again = rig.bearer_update(ctx["note"]["id"], ctx["note"]["updated"], body="另一個")
    assert again.status_code == 202
    _expire_all(rig)
    rig.queue()
    assert rig.row(ctx["note"]["id"]) == ctx["before"]


def test_legacy_note_created_before_migration_is_agent_and_updates_directly(
    tmp_path,
):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.row_factory = sqlite3.Row
    try:
        migrate(raw, migrations=MIGRATIONS[:19])
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created, space) "
            "VALUES ('folder/legacy', 'legacy', 'repo', '2026-01-01T00:00:00.000Z', 'dev')"
        )
        raw.execute(
            "INSERT INTO notes (id, vault, title, body, created, updated, principal, "
            "updated_by_principal) VALUES ('old1', 'folder/legacy', 't', 'b', "
            "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', 'owner', 'owner')"
        )
    finally:
        raw.close()
    with SpaceClient(create_app(make_settings(path))) as client:
        client.headers.update(AUTH)
        got = client.post(
            "/v1/get", json={"vault": "folder/legacy", "ids": ["old1"]}
        ).json()["items"][0]
        resp = client.post(
            "/v1/update",
            json={
                "vault": "folder/legacy",
                "id": "old1",
                "expected_updated": got["updated"],
                "body": "agent 直接改舊筆記",
            },
        )
        assert resp.status_code == 200, resp.text
        assert "proposal" not in resp.json()
        plan = client.post(
            "/v1/note_delete",
            json={"space": "dev", "vault": "folder/legacy", "id": "old1"},
        )
        assert plan.status_code == 200  # 舊筆記的刪除仍走提案（刪除不看 write_origin）
        assert plan.json()["proposal"]["kind"] == "note_delete"


# ── 5. 連結邊界 ──


def test_link_reorder_and_duplicates_are_not_removals(rig):
    b, c = rig.note(title="乙"), rig.note(title="丙")
    a = rig.note(title="甲", links=[b["id"], c["id"]])
    resp = rig.bearer_update(a["id"], a["updated"], links=[c["id"], b["id"], c["id"]])
    assert resp.status_code == 200 and "proposal" not in resp.json()
    assert set(_get(rig, a["id"])["links"]) == {b["id"], c["id"]}
    assert rig.proposals() == []


def test_removing_a_body_derived_link_explicitly_does_not_silently_drop_it(rig):
    b = rig.note(title="乙")
    a = rig.note(title="甲", body="見 [[乙]]")
    resp = rig.bearer_update(a["id"], a["updated"], links=[])
    # body 仍寫著 [[乙]]：連結由 body 重新衍生，不算明確移除、不送審，也不丟失
    assert resp.status_code == 200 and "proposal" not in resp.json()
    assert b["id"] in _get(rig, a["id"])["links"]
    assert rig.proposals() == []


def test_removal_and_supersedes_change_share_one_link_change_proposal(rig):
    old1, old2 = rig.note(title="舊一"), rig.note(title="舊二")
    b = rig.note(title="乙")
    a = rig.note(title="甲", links=[b["id"]], supersedes=old1["id"])
    resp = rig.bearer_update(a["id"], a["updated"], links=[], supersedes=old2["id"])
    assert resp.status_code == 202, resp.text
    props = rig.proposals(kind="link_change")
    assert len(props) == 1
    assert json.loads(props[0]["payload"]) == {
        "remove": [b["id"]],
        "supersedes": {"from": old1["id"], "to": old2["id"]},
    }
    assert rig.proposals(kind="note_update") == []
    approved = rig.approve(props[0]["id"])
    assert approved.status_code == 200, approved.text
    row = rig.row(a["id"])
    assert json.loads(row["links"]) == [] and row["supersedes"] == old2["id"]


def test_partial_apply_reports_both_held_parts(rig):
    old1, old2 = rig.note(title="舊一"), rig.note(title="舊二")
    b = rig.note(title="乙")
    a = rig.note(title="甲", links=[b["id"]], supersedes=old1["id"])
    resp = rig.bearer_update(
        a["id"], a["updated"], title="改名", links=[], supersedes=old2["id"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["partial"] is True and body["proposal_pending"] is True
    assert body["held"] == {"links_removed": [b["id"]], "supersedes_changed": True}
    row = rig.row(a["id"])
    assert row["title"] == "改名" and row["supersedes"] == old1["id"]
    assert json.loads(row["links"]) == [b["id"]]


def test_protected_note_link_removal_is_one_note_update_not_split(rig):
    b = rig.note(title="乙")
    a = rig.note(title="甲", links=[b["id"]])
    ui = rig.ui_edit(a["id"], a["updated"], title="人類改名")
    before = rig.row(a["id"])
    resp = rig.bearer_update(a["id"], ui["updated"], links=[], title="agent 改名")
    assert resp.status_code == 202
    assert resp.json()["proposal"]["kind"] == "note_update"
    assert rig.proposals(kind="link_change") == []
    assert rig.row(a["id"]) == before


def test_link_change_approval_keeps_links_added_meanwhile_and_is_idempotent(rig):
    b, c, d = rig.note(title="乙"), rig.note(title="丙"), rig.note(title="丁")
    a = rig.note(title="甲", links=[b["id"], c["id"]])
    prop = rig.bearer_update(a["id"], a["updated"], links=[c["id"]]).json()["proposal"]
    _external_update(rig, a["id"], {"links": (b["id"], c["id"], d["id"])})
    assert rig.approve(prop["id"]).status_code == 200
    assert set(json.loads(rig.row(a["id"])["links"])) == {c["id"], d["id"]}
    # 目標連結早已不在：核准視為完成，不寫 note
    x = rig.note(title="戊", links=[b["id"]])
    p2 = rig.bearer_update(x["id"], x["updated"], links=[]).json()["proposal"]
    _external_update(rig, x["id"], {"links": ()})
    before = rig.row(x["id"])
    done = rig.approve(p2["id"])
    assert done.status_code == 200 and done.json()["proposal"]["state"] == "applied"
    assert rig.row(x["id"]) == before


def test_supersedes_change_approval_does_not_clobber_a_newer_value(rig):
    old1, old2, other = (rig.note(title=n) for n in ("舊一", "舊二", "別的"))
    a = rig.note(title="甲", supersedes=old1["id"])
    prop = rig.bearer_update(a["id"], a["updated"], supersedes=old2["id"]).json()[
        "proposal"
    ]
    _external_update(rig, a["id"], {"supersedes": other["id"]})
    assert rig.approve(prop["id"]).status_code == 200
    assert rig.row(a["id"])["supersedes"] == other["id"]


def test_update_with_stale_expected_updated_makes_no_proposal(rig):
    note = rig.protected_note()
    _code(
        rig.bearer_update(note["id"], "2000-01-01T00:00:00.000Z", body="x"),
        409,
        "version_conflict",
    )
    assert rig.proposals() == []


# ── 6. 範圍 ──


def test_personal_space_is_never_governed(rig):
    pv = "personal/prop-self"
    create_vault(rig.owner, pv, space="personal")
    made = rig.owner.post(
        "/v1/write",
        json={"space": "personal", "vault": pv, "title": "私人", "body": "b"},
    )
    assert made.status_code == 201, made.text
    nid = made.json()["id"]
    edited = rig.ui_edit(
        nid, made.json()["updated"], vault=pv, space="personal", body="我改"
    )
    assert rig.row(nid)["write_origin"] == "ui"
    direct = rig.owner.post(
        "/v1/update",
        json={
            "space": "personal",
            "vault": pv,
            "id": nid,
            "expected_updated": edited["updated"],
            "body": "agent 直接改",
        },
    )
    assert direct.status_code == 200, direct.text
    assert "proposal" not in direct.json()
    plan = rig.owner.post(
        "/v1/note_delete", json={"space": "personal", "vault": pv, "id": nid}
    )
    assert plan.status_code == 200 and "proposal" not in plan.json()
    done = rig.owner.post(
        "/v1/note_delete",
        json={
            "space": "personal",
            "vault": pv,
            "id": nid,
            "confirm_token": plan.json()["confirm_token"],
        },
    )
    assert done.status_code == 200 and done.json()["executed"] is True
    assert rig.proposals() == []


def test_lore_space_is_governed_for_regular_agents_and_isolated_from_dev(rig):
    note = rig.owner.post(
        "/v1/write",
        json={"space": "lore", "vault": LORE, "title": "世界", "body": "b"},
    ).json()
    edited = rig.ui_edit(
        note["id"], note["updated"], vault=LORE, space="lore", body="人類改"
    )
    held = rig.owner.post(
        "/v1/update",
        json={
            "space": "lore",
            "vault": LORE,
            "id": note["id"],
            "expected_updated": edited["updated"],
            "body": "agent 想改",
        },
    )
    assert held.status_code == 202, held.text
    prop = held.json()["proposal"]
    # 用 dev 範圍核准 lore 的提案 → 找不到（不洩漏）
    wrong = rig.approve(prop["id"], vault=LORE, space="dev")
    assert wrong.status_code == 404, wrong.text
    assert rig.proposals(id=prop["id"])[0]["state"] == "pending"
    assert rig.queue()["items"] == []
    lore_items = rig.ui.post(
        "/v1/review/queue", json={"space": "lore", "vault": "*"}
    ).json()["items"]
    assert [i["item_id"] for i in lore_items] == [prop["id"]]
    ok = rig.approve(prop["id"], vault=LORE, space="lore")
    assert ok.status_code == 200, ok.text
    assert rig.row(note["id"])["body"] == "agent 想改"


def test_uep_lore_pending_contract_unchanged_by_proposals(rig):
    uep = SpaceClient(rig.ui.app)
    uep.__enter__()
    try:
        uep.headers.update({"Authorization": f"Bearer {UEP_TOKEN}"})
        made = uep.post(
            "/v1/write",
            json={"space": "lore", "vault": LORE, "title": "UEP", "body": "b"},
        ).json()
        got = rig.ui.post(
            "/v1/get", json={"space": "lore", "vault": LORE, "ids": [made["id"]]}
        ).json()["items"][0]
        assert got["review_state"] == "pending"
        upd = uep.post(
            "/v1/update",
            json={
                "space": "lore",
                "vault": LORE,
                "id": made["id"],
                "expected_updated": made["updated"],
                "body": "uep 自己改",
            },
        )
        assert upd.status_code == 200, upd.text
        assert "proposal" not in upd.json()
        assert rig.proposals() == []
    finally:
        uep.__exit__(None, None, None)


# ── 7. 保護的負向測試（拿掉保護 → 不變量測試轉紅）──
#
# 每個 invariant 先在正常實作下通過，再 monkeypatch 拿掉對應保護，斷言它會失敗。


def _must_go_red(invariant, monkeypatch, patch) -> None:
    invariant()  # 正常實作必須通過，否則這個負向測試沒有意義
    patch(monkeypatch)
    with pytest.raises(AssertionError):
        invariant()


def test_negative_revision_check_removed_lets_wrong_rev_apply(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        prop = ctx["resp"]["proposal"]
        resp = rig.approve(prop["id"], rev=99)
        assert resp.status_code == 409, resp.text
        assert rig.row(ctx["note"]["id"]) == ctx["before"]

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(
            proposals_service, "_require_open_pending", lambda *a: None
        ),
    )


def _patch_always_rebase(mp) -> None:
    """模擬「拿掉基準衝突偵測」：核准一律當成已明確選擇以目前版本為基準。"""
    original = proposals_service._apply

    def no_stale(conn, row, reviewer, rebase_to):
        note = proposals_service.get_note(
            conn, row.vault, row.target_id, space=row.space
        )
        return original(conn, row, reviewer, note.updated)

    mp.setattr(proposals_service, "_apply", no_stale)


def test_negative_stale_detection_removed_lets_overwrite_happen(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        _external_update(rig, ctx["note"]["id"], {"body": "別人改的"})
        assert rig.approve(ctx["resp"]["proposal"]["id"]).status_code == 409
        assert rig.row(ctx["note"]["id"])["body"] == "別人改的"

    _must_go_red(
        invariant,
        monkeypatch,
        _patch_always_rebase,
    )


def test_negative_ui_only_guard_removed_lets_bearer_approve(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        prop = ctx["resp"]["proposal"]
        resp = rig.owner.post(
            "/v1/review/proposal_approve",
            json={"space": "dev", "vault": DEV, "id": prop["id"], "expected_rev": 1},
        )
        assert resp.status_code == 403, resp.text
        assert rig.row(ctx["note"]["id"]) == ctx["before"]

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(
            guards, "auth_method_of", lambda request: principals_mod.AUTH_UI
        ),
    )


def test_negative_approved_apply_not_marking_ui_origin_is_caught(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        assert rig.approve(ctx["resp"]["proposal"]["id"]).status_code == 200
        assert rig.row(ctx["note"]["id"])["write_origin"] == "ui"

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(t.Actor, "write_origin", property(lambda self: "agent")),
    )


def test_negative_dropping_unique_index_lets_duplicate_open_rows_in(rig):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        existing = rig.proposals(id=ctx["resp"]["proposal"]["id"])[0]
        cols = list(existing)
        dup = dict(existing, id="prop_dup_" + ctx["note"]["id"])
        conn = connect(rig.db_path)
        try:
            try:
                conn.execute(
                    f"INSERT INTO review_proposals ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [dup[c] for c in cols],
                )
            except sqlite3.IntegrityError:
                return
            raise AssertionError("資料庫沒有擋下第二筆開放提案")
        finally:
            conn.close()

    invariant()
    _sql(rig, "DROP INDEX review_proposals_open")
    with pytest.raises(AssertionError):
        invariant()


def test_negative_ui_direct_edit_not_closing_proposals_is_caught(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        note = ctx["note"]
        rig.ui_edit(note["id"], rig.row(note["id"])["updated"], title="使用者自己改")
        row = rig.proposals(id=ctx["resp"]["proposal"]["id"])[0]
        assert (row["state"], row["resolved_via"]) == ("superseded", "ui_direct")

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(store, "close_for_target", lambda *a, **k: 0),
    )


def test_negative_kind_guard_removed_lets_bearer_claim_a_deletion(rig, monkeypatch):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        resp = rig.owner.post(
            "/v1/proposal_claim",
            json={
                "vault": DEV,
                "id": ctx["resp"]["proposal"]["id"],
                "expect": "pending",
                "plan_digest": "x",
            },
        )
        assert resp.status_code == 403, resp.text

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(proposals_service, "_require_archive", lambda row: None),
    )


def test_negative_lazy_expiry_removed_lets_expired_proposal_be_approved(
    rig, monkeypatch
):
    def invariant():
        ctx = _hold_invariant_fresh(rig)
        _expire_all(rig)
        assert rig.approve(ctx["resp"]["proposal"]["id"]).status_code == 409
        assert rig.row(ctx["note"]["id"]) == ctx["before"]

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(store, "expire_due", lambda *a, **k: 0),
    )


def test_negative_race_invariant_goes_red_when_approve_does_not_close_proposal(
    rig, monkeypatch
):
    """競態的終態不變量（提案 applied、rev=2）在核准不結案時會紅。"""

    def invariant():
        note, plan = _delete_flow(rig)
        prop_id = plan["proposal"]["id"]
        rig.approve(prop_id)
        final = rig.proposals(id=prop_id)[0]
        assert (final["state"], final["rev"]) == ("applied", 2)

    _must_go_red(
        invariant,
        monkeypatch,
        lambda mp: mp.setattr(proposals_service, "_mark_applied", lambda *a, **k: None),
    )
