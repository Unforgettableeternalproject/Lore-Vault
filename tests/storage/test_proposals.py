"""待審提案儲存層與領域政策（schema v20）：條件式轉移、去重、過期、租約回復、
政策矩陣；doctor 三項對帳各有「破壞後轉紅」的測試。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.doctor.framework import Status
from lore_vault.proposals import policy
from lore_vault.proposals import service as proposals_service
from lore_vault.proposals import types as t
from lore_vault.proposals.types import Actor
from lore_vault.schema import Note
from lore_vault.storage import proposals as store
from lore_vault.storage.notes import insert_note
from lore_vault.storage.timeutil import utc_now

V = "folder/p"
LORE = "lore/p"
TS = "2026-09-01T00:00:00.000Z"


@pytest.fixture
def vault(add_vault):
    add_vault(V)
    return V


def _note(conn, note_id="n1", *, origin="agent", links=(), supersedes=None) -> Note:
    return insert_note(
        conn,
        V,
        Note(
            id=note_id,
            vault=V,
            title="標題",
            body="內文",
            created=TS,
            updated=TS,
            principal="owner",
            updated_by_principal="owner",
            links=links,
            supersedes=supersedes,
            write_origin=origin,
        ),
        space="dev",
    )


def _up(conn, **kw):
    args = {
        "space": "dev",
        "vault": V,
        "kind": t.KIND_NOTE_UPDATE,
        "target_id": "n1",
        "payload": {"changes": {"body": "新"}},
        "proposer_principal": "owner",
    }
    args.update(kw)
    return store.upsert_open(conn, **args)


# ── 儲存層 ──


def test_transition_is_conditional_and_bumps_rev(conn, vault):
    prop = _up(conn).proposal
    assert (prop.state, prop.rev) == ("pending", 1)
    assert not store.transition(
        conn, prop.id, expect_state="approved", to_state="applied"
    )
    assert not store.transition(
        conn, prop.id, expect_state="pending", expect_rev=9, to_state="applied"
    )
    assert store.transition(
        conn,
        prop.id,
        expect_state="pending",
        expect_rev=1,
        to_state="rejected",
        via="ui",
        by="me",
        reason="no",
    )
    done = store.get(conn, prop.id)
    assert (done.state, done.rev, done.resolved_by, done.resolved_via) == (
        "rejected",
        2,
        "me",
        "ui",
    )
    # 第二次同樣的轉移失敗（沒有 check-then-act）
    assert not store.transition(
        conn, prop.id, expect_state="pending", expect_rev=1, to_state="rejected"
    )


def test_dedupe_does_not_touch_rev_and_supersede_chains(conn, vault):
    first = _up(conn).proposal
    again = _up(conn)
    assert again.deduped and again.proposal.id == first.id and again.proposal.rev == 1
    newer = _up(conn, payload={"changes": {"body": "另一個"}})
    assert not newer.deduped and newer.superseded.id == first.id
    old = store.get(conn, first.id)
    assert (old.state, old.superseded_by) == ("superseded", newer.proposal.id)
    assert store.find_open(conn, V, t.KIND_NOTE_UPDATE, "n1").id == newer.proposal.id


def test_partial_unique_index_is_the_last_line_of_defense(conn, vault):
    _up(conn)
    cols = (
        "id, space, vault, kind, target_type, target_id, payload, payload_digest,"
        " proposer_principal, state, rev, created_at, updated_at, expires_at"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO review_proposals ({cols}) VALUES "
            "('dup','dev',?, 'note_update','note','n1','{}','x','o','pending',1,?,?,?)",
            (V, TS, TS, TS),
        )
    # 終態不佔位：同目標可再開新提案
    prop = store.find_open(conn, V, t.KIND_NOTE_UPDATE, "n1")
    store.transition(conn, prop.id, expect_state="pending", to_state="rejected")
    assert not _up(conn).deduped


def test_queue_limit_counts_only_open(conn, vault):
    _up(conn, target_id="a", max_open=2)
    _up(conn, target_id="b", max_open=2)
    with pytest.raises(t.ProposalQueueFull):
        _up(conn, target_id="c", max_open=2)
    # 同目標改寫不增加開放數
    assert not _up(
        conn, target_id="a", payload={"changes": {"body": "z"}}, max_open=2
    ).deduped


def test_expire_due_and_close_for_target(conn, vault):
    prop = _up(conn, now="2020-01-01T00:00:00.000Z", ttl_days=1).proposal
    other = _up(
        conn, kind=t.KIND_LINK_CHANGE, payload={"remove": ["x"], "supersedes": None}
    ).proposal
    # 第二次 upsert 已順手做了惰性過期；再跑一次是 no-op
    assert store.get(conn, prop.id).state == "expired"
    assert store.expire_due(conn) == 0
    assert (
        store.close_for_target(
            conn, V, "n1", to_state="obsolete", via="system", reason="gone"
        )
        == 1
    )
    assert store.get(conn, other.id).state == "obsolete"
    assert store.find_open(conn, V, t.KIND_LINK_CHANGE, "n1") is None


def test_claim_lease_restores_previous_state(conn, vault):
    reg = proposals_service.register_archive(
        conn,
        vault=V,
        name="chg",
        payload={"plan_digest": "d"},
        proposer_principal="owner",
        proposer_author=None,
    ).proposal
    store.transition(
        conn, reg.id, expect_state="pending", to_state="approved", via="ui", by="me"
    )
    row, claim_id = proposals_service.claim(
        conn, reg.id, expect="approved", plan_digest="d"
    )
    assert (row.state, row.claim_prev_state) == ("executing", "approved")
    with pytest.raises(t.ProposalResolved):
        proposals_service.claim(conn, reg.id, expect="approved", plan_digest="d")
    # 租約逾時 → 回 approved，租約欄位清掉
    assert store.expire_due(conn, now="2099-01-01T00:00:00.000Z") >= 1
    back = store.get(conn, reg.id)
    assert back.state in ("approved", "expired") and back.claim_id is None
    assert claim_id


def test_close_claim_applied_records_via_and_wrong_claim_is_resolved(conn, vault):
    reg = proposals_service.register_archive(
        conn,
        vault=V,
        name="chg",
        payload={"plan_digest": "d"},
        proposer_principal="owner",
        proposer_author=None,
    ).proposal
    _, claim_id = proposals_service.claim(
        conn, reg.id, expect="pending", plan_digest="d"
    )
    with pytest.raises(t.ProposalResolved):
        proposals_service.close_claim(conn, reg.id, claim_id="nope", outcome="applied")
    done = proposals_service.close_claim(
        conn, reg.id, claim_id=claim_id, outcome="applied"
    )
    assert (done.state, done.resolved_via) == ("applied", "token")


def test_non_archive_proposals_are_rejected_by_agent_side_services(conn, vault):
    _note(conn)
    prop = _up(conn).proposal
    with pytest.raises(t.ProposalKindForbidden):
        proposals_service.claim(conn, prop.id, expect="pending", plan_digest="x")
    with pytest.raises(t.ProposalKindForbidden):
        proposals_service.close_claim(conn, prop.id, claim_id="c", outcome="applied")


# ── 政策矩陣（純函式）──


def _n(origin="agent", links=(), supersedes=None):
    return Note(
        id="n",
        vault=V,
        title="t",
        body="b",
        created=TS,
        updated=TS,
        links=links,
        supersedes=supersedes,
        write_origin=origin,
    )


AGENT = Actor.agent("owner")


@pytest.mark.parametrize(
    "actor,space,governed",
    [
        (AGENT, "dev", True),
        (AGENT, "lore", True),
        (AGENT, "personal", False),
        (Actor.agent("uep"), "dev", False),
        (Actor.ui("owner"), "dev", False),
        (Actor.system("owner"), "dev", False),
        (Actor.system_apply("owner"), "dev", False),
    ],
)
def test_policy_scope(actor, space, governed):
    assert policy.is_governed(actor, space) is governed
    assert policy.decide_delete(actor, space) is governed


def test_policy_update_matrix():
    ui_note = _n(origin="ui")
    assert (
        policy.decide_update(AGENT, "dev", ui_note, {"body": "x"}).kind
        == t.KIND_NOTE_UPDATE
    )
    assert not policy.decide_update(AGENT, "dev", _n(), {"body": "x"}).hold
    held = policy.decide_update(
        AGENT, "dev", _n(links=("a",)), {"links": ()}, removed_links=("a",)
    )
    assert (held.hold, held.kind, held.partial) == (True, t.KIND_LINK_CHANGE, False)
    part = policy.decide_update(
        AGENT,
        "dev",
        _n(links=("a",)),
        {"links": ("b",), "title": "t2"},
        removed_links=("a",),
    )
    assert part.partial and part.remaining == {"links": ("b", "a"), "title": "t2"}
    # supersedes：首次設定不送審，改值或清空才送審
    assert not policy.decide_update(AGENT, "dev", _n(), {"supersedes": "x"}).hold
    moved = policy.decide_update(AGENT, "dev", _n(supersedes="x"), {"supersedes": "y"})
    assert moved.payload["supersedes"] == {"from": "x", "to": "y"}
    # 政策範圍外一律放行
    assert not policy.decide_update(Actor.ui("o"), "dev", ui_note, {"body": "x"}).hold
    assert not policy.decide_update(
        Actor.agent("uep"), "dev", ui_note, {"body": "x"}
    ).hold


# ── doctor ──


def _run(conn, name):
    report = default_registry().run(
        DoctorContext(
            settings={"now": datetime(2026, 9, 2, tzinfo=UTC)}, resources={"db": conn}
        ),
        categories=[name.split(".")[0]],
    )
    return next(o.result for o in report.outcomes if o.name == name)


def test_doctor_clean_database_is_green(conn, vault):
    _note(conn)
    for name in ("proposals.integrity", "proposals.expiry", "notes.write_origin"):
        assert _run(conn, name).status is Status.PASS


def test_doctor_integrity_detects_open_proposal_with_missing_target(conn, vault):
    _up(conn, target_id="ghost")
    res = _run(conn, "proposals.integrity")
    assert res.status is Status.FAIL and res.counts["open_target_missing"] == 1


def test_doctor_integrity_detects_duplicate_open_when_index_dropped(conn, vault):
    _note(conn)
    prop = _up(conn).proposal
    conn.execute("DROP INDEX review_proposals_open")
    conn.execute(
        "INSERT INTO review_proposals SELECT 'dup', space, vault, kind, target_type,"
        " target_id, payload, 'other', base_updated, base_snapshot, proposer_principal,"
        " proposer_author, state, rev, created_at, updated_at, expires_at, resolved_by,"
        " resolved_at, resolved_via, resolution_reason, superseded_by, claim_id,"
        " claim_expires_at, claim_prev_state, applied_updated FROM review_proposals"
        " WHERE id = ?",
        (prop.id,),
    )
    res = _run(conn, "proposals.integrity")
    assert res.status is Status.FAIL and res.counts["duplicate_open"] == 1


def test_doctor_integrity_detects_applied_edit_not_ui_and_older_target(conn, vault):
    note = _note(conn)
    prop = _up(conn).proposal
    store.transition(
        conn,
        prop.id,
        expect_state="pending",
        to_state="applied",
        via="ui",
        by="me",
        applied_updated=note.updated,
    )
    res = _run(conn, "proposals.integrity")  # updated 相同卻不是 ui 來源
    assert res.status is Status.FAIL and res.counts["applied_not_ui"] == 1
    conn.execute("UPDATE notes SET write_origin = 'ui' WHERE id = 'n1'")
    assert _run(conn, "proposals.integrity").status is Status.PASS
    # 之後合法寫入讓 updated 前進：不算違規
    conn.execute(
        "UPDATE notes SET updated = '2026-09-05T00:00:00.000Z', write_origin = 'agent'"
    )
    assert _run(conn, "proposals.integrity").status is Status.PASS
    conn.execute(
        "UPDATE review_proposals SET applied_updated = '2027-01-01T00:00:00.000Z'"
    )
    assert _run(conn, "proposals.integrity").counts["applied_target_older"] == 1


def test_doctor_integrity_detects_expired_lease_and_space_mismatch(conn, vault):
    reg = proposals_service.register_archive(
        conn,
        vault=V,
        name="chg",
        payload={"plan_digest": "d"},
        proposer_principal="owner",
        proposer_author=None,
    ).proposal
    proposals_service.claim(conn, reg.id, expect="pending", plan_digest="d")
    conn.execute(
        "UPDATE review_proposals SET claim_expires_at = '2020-01-01T00:00:00.000Z'"
    )
    conn.execute("UPDATE review_proposals SET space = 'lore'")
    res = _run(conn, "proposals.integrity")
    assert res.counts["lease_expired"] == 1 and res.counts["space_mismatch"] == 1


def test_doctor_output_never_contains_payload_text(conn, vault):
    _up(conn, target_id="ghost", payload={"changes": {"body": "機密內文不可出現"}})
    res = _run(conn, "proposals.integrity")
    assert "機密內文不可出現" not in repr(res)


def test_doctor_expiry_warns_on_overdue_and_aged(conn, vault):
    _note(conn)
    _up(conn, now="2026-08-01T00:00:00.000Z", ttl_days=1)
    res = _run(conn, "proposals.expiry")
    assert res.status is Status.WARN and res.counts["overdue"] == 1
    conn.execute("UPDATE review_proposals SET expires_at = '2026-12-01T00:00:00.000Z'")
    res = _run(conn, "proposals.expiry")
    assert res.status is Status.WARN and res.counts["aged"] == 1


def test_doctor_write_origin_detects_bypassed_check_and_missing_editor(conn, vault):
    _note(conn)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute("UPDATE notes SET write_origin = 'robot'")
    res = _run(conn, "notes.write_origin")
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    assert res.status is Status.FAIL and res.counts["invalid_origin"] == 1
    conn.execute("UPDATE notes SET write_origin = 'ui', updated_by_principal = NULL")
    res = _run(conn, "notes.write_origin")
    assert res.status is Status.FAIL and res.counts["ui_without_editor"] == 1


def test_utc_now_format_is_comparable():
    assert utc_now() > "2020-01-01T00:00:00.000Z"
