"""task_archive 的 claim 租約（破壞性驗證）：用注入的時間軸測互斥、逾時重新 claim、
release 還原，以及過期與租約的交互。不依賴 wall clock，也不 sleep。"""

from __future__ import annotations

import threading

import pytest

from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.storage import proposals as store
from lore_vault.storage.db import connect
from lore_vault.storage.timeutil import format_utc, parse_utc

V = "folder/lease"
T0 = "2026-10-01T00:00:00.000Z"
LEASE = t.CLAIM_LEASE_SECONDS


@pytest.fixture
def vault(add_vault):
    add_vault(V)
    return V


def _at(seconds: float) -> str:
    from datetime import timedelta

    return format_utc(parse_utc(T0) + timedelta(seconds=seconds))


def _register(conn, name="chg", digest="d", now=T0):
    up = store.upsert_open(
        conn,
        space="dev",
        vault=V,
        kind=t.KIND_TASK_ARCHIVE,
        target_id=name,
        payload={"plan_digest": digest},
        proposer_principal="owner",
        now=now,
    )
    return up.proposal


def _approve(conn, prop, now=T0):
    assert store.transition(
        conn,
        prop.id,
        expect_state=t.PENDING,
        expect_rev=prop.rev,
        to_state=t.APPROVED,
        via=t.VIA_UI,
        by="me",
        now=now,
    )


def test_two_claims_are_mutually_exclusive_until_lease_runs_out(conn, vault):
    prop = _register(conn)
    row, first = proposals_service.claim(
        conn, prop.id, expect="pending", plan_digest="d", now=T0
    )
    assert row.claim_expires_at == _at(LEASE)
    for expect in ("pending", "approved"):
        with pytest.raises(t.ProposalResolved):
            proposals_service.claim(
                conn, prop.id, expect=expect, plan_digest="d", now=_at(LEASE - 1)
            )
    assert store.get(conn, prop.id).claim_id == first  # 輸家沒動到租約


def test_lease_expiry_allows_reclaim_and_old_holder_is_locked_out(conn, vault):
    prop = _register(conn)
    _, old = proposals_service.claim(
        conn, prop.id, expect="pending", plan_digest="d", now=T0
    )
    # 恰在逾時那一刻（<=）即視為逾時；新的 claim 贏
    row, new = proposals_service.claim(
        conn, prop.id, expect="pending", plan_digest="d", now=_at(LEASE)
    )
    assert new != old and row.state == t.EXECUTING
    assert row.claim_expires_at == _at(LEASE + LEASE)
    # 舊持有者晚到：不論 applied 或 release 都不行，也不能影響新租約
    for outcome in ("applied", "release"):
        with pytest.raises(t.ProposalResolved):
            proposals_service.close_claim(
                conn, prop.id, claim_id=old, outcome=outcome, now=_at(LEASE + 1)
            )
    still = store.get(conn, prop.id)
    assert (still.state, still.claim_id) == (t.EXECUTING, new)
    done = proposals_service.close_claim(
        conn, prop.id, claim_id=new, outcome="applied", now=_at(LEASE + 2)
    )
    assert (done.state, done.resolved_via) == (t.APPLIED, t.VIA_TOKEN)
    assert done.claim_id is None and done.claim_expires_at is None


def test_one_second_before_expiry_the_lease_still_holds(conn, vault):
    prop = _register(conn)
    proposals_service.claim(conn, prop.id, expect="pending", plan_digest="d", now=T0)
    with pytest.raises(t.ProposalResolved):
        proposals_service.claim(
            conn, prop.id, expect="pending", plan_digest="d", now=_at(LEASE - 0.001)
        )


def test_release_restores_the_state_before_claim_for_both_paths(conn, vault):
    pend = _register(conn, "a")
    appr = _register(conn, "b")
    _approve(conn, appr)
    appr = store.get(conn, appr.id)
    for prop, expect in ((pend, "pending"), (appr, "approved")):
        _, cid = proposals_service.claim(
            conn, prop.id, expect=expect, plan_digest="d", now=T0
        )
        back = proposals_service.close_claim(
            conn, prop.id, claim_id=cid, outcome="release", now=_at(5)
        )
        assert back.state == expect
        assert (back.claim_id, back.claim_expires_at, back.claim_prev_state) == (
            None,
            None,
            None,
        )
        # 還原後可以被重新 claim，且仍要求原本的 expect
        wrong = "approved" if expect == "pending" else "pending"
        with pytest.raises(t.ProposalResolved):
            proposals_service.claim(
                conn, prop.id, expect=wrong, plan_digest="d", now=_at(6)
            )
        proposals_service.claim(
            conn, prop.id, expect=expect, plan_digest="d", now=_at(7)
        )


def test_approved_consent_survives_a_lapsed_lease(conn, vault):
    prop = _register(conn)
    _approve(conn, prop)
    prop = store.get(conn, prop.id)
    proposals_service.claim(conn, prop.id, expect="approved", plan_digest="d", now=T0)
    assert store.expire_due(conn, now=_at(LEASE + 1)) == 1
    back = store.get(conn, prop.id)
    assert (back.state, back.resolved_via, back.resolved_by) == (
        t.APPROVED,
        t.VIA_UI,
        "me",
    )  # 使用者的同意紀錄還在
    # 重新 claim（approved 路徑）成功，close 記為 ui 來源
    _, cid = proposals_service.claim(
        conn, prop.id, expect="approved", plan_digest="d", now=_at(LEASE + 2)
    )
    done = proposals_service.close_claim(
        conn, prop.id, claim_id=cid, outcome="applied", now=_at(LEASE + 3)
    )
    assert done.resolved_via == t.VIA_UI


def test_executing_proposal_is_not_expired_by_ttl_until_lease_returns(conn, vault):
    prop = _register(conn)
    ttl_end = parse_utc(prop.expires_at)
    from datetime import timedelta

    # 在 TTL 到期前 1 秒 claim；租約跨過 TTL 終點
    near = format_utc(ttl_end - timedelta(seconds=1))
    proposals_service.claim(conn, prop.id, expect="pending", plan_digest="d", now=near)
    mid = format_utc(ttl_end + timedelta(seconds=10))
    assert store.expire_due(conn, now=mid) == 0  # 租約還有效：不能被 TTL 掃掉
    assert store.get(conn, prop.id).state == t.EXECUTING
    after = format_utc(ttl_end + timedelta(seconds=LEASE + 10))
    assert store.expire_due(conn, now=after) == 1  # 租約逾時回 pending
    assert store.get(conn, prop.id).state == t.PENDING
    assert store.expire_due(conn, now=after) == 1  # 下一輪才依 TTL 轉 expired
    assert store.get(conn, prop.id).state == t.EXPIRED


def test_claim_after_ttl_expiry_and_with_wrong_digest_is_refused(conn, vault):
    prop = _register(conn)
    late = format_utc(parse_utc(prop.expires_at))
    with pytest.raises(t.ProposalResolved):
        proposals_service.claim(
            conn, prop.id, expect="pending", plan_digest="d", now=late
        )
    # 拒絕的交易會連同惰性過期一起 rollback：狀態仍是 pending，但絕不會是 executing
    assert store.get(conn, prop.id).state == t.PENDING
    assert store.expire_due(conn, now=late) == 1
    assert store.get(conn, prop.id).state == t.EXPIRED
    fresh = _register(conn, "other", now=T0)
    with pytest.raises(t.ProposalStateConflict):
        proposals_service.claim(
            conn, fresh.id, expect="pending", plan_digest="不同", now=T0
        )
    assert store.get(conn, fresh.id).state == t.PENDING  # 失敗不留租約


def test_only_executing_lease_with_matching_claim_can_close(conn, vault):
    prop = _register(conn)
    with pytest.raises(t.ProposalResolved):  # 還沒 claim
        proposals_service.close_claim(
            conn, prop.id, claim_id="claim_x", outcome="release"
        )
    with pytest.raises(ValueError):
        proposals_service.close_claim(conn, prop.id, claim_id="c", outcome="whatever")
    with pytest.raises(ValueError):
        proposals_service.claim(conn, prop.id, expect="executing", plan_digest="d")


def test_threaded_claims_with_separate_connections_have_one_winner(
    db_path, vault, conn
):
    prop = _register(conn)
    _approve(conn, prop)
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(expect):
        c = connect(db_path)
        try:
            barrier.wait(timeout=20)
            try:
                proposals_service.claim(
                    c, prop.id, expect=expect, plan_digest="d", now=T0
                )
                outcomes.append("won")
            except t.ProposalResolved:
                outcomes.append("lost")
        finally:
            c.close()

    threads = [
        threading.Thread(target=attempt, args=("approved" if i % 2 else "pending",))
        for i in range(8)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    # 提案已是 approved：只有 expect=approved 的可能贏，且只有一個
    assert outcomes.count("won") == 1 and outcomes.count("lost") == 7
    assert store.get(conn, prop.id).claim_prev_state == t.APPROVED
