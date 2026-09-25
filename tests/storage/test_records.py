"""Episode／Concept／Injection：vault 凍結、三態欄位原樣讀回、冪等重送。"""

from __future__ import annotations

import pytest

from lore_vault.schema import MISSING, Concept, Injection
from lore_vault.storage import records
from lore_vault.storage.errors import DuplicateRecord

V = "folder/rec"


@pytest.fixture
def vault(add_vault):
    return add_vault(V)


def test_episode_round_trip_and_idempotent_resend(conn, vault, make_episode):
    ep = make_episode()
    assert records.insert_episode(conn, V, ep) is True
    assert records.insert_episode(conn, V, ep) is False  # spool 重送
    assert records.list_episodes(conn, V) == ([ep], None)
    assert records.count_episodes(conn, V) == 1


def test_episode_same_key_different_content_is_rejected(
    conn, vault, add_vault, make_episode
):
    records.insert_episode(conn, V, make_episode())
    with pytest.raises(DuplicateRecord):
        records.insert_episode(conn, V, make_episode(user_text="被改過"))
    other = add_vault("folder/other")
    with pytest.raises(DuplicateRecord):
        records.insert_episode(conn, other, make_episode())


def test_episode_injected_three_states(conn, vault, make_episode):
    missing = make_episode(prompt_id="p-missing")
    empty = make_episode(prompt_id="p-empty", injected=[])
    filled = make_episode(prompt_id="p-filled", injected=["c-1"])
    assert missing.injected is MISSING
    for ep in (missing, empty, filled):
        records.insert_episode(conn, V, ep)
    got = {e.prompt_id: e for e in records.list_episodes(conn, V)[0]}
    assert got["p-missing"].injected is MISSING
    assert got["p-empty"].injected == ()
    assert got["p-filled"].injected == ("c-1",)


def test_episode_timestamps_normalized_and_filterable(conn, vault, make_episode):
    records.insert_episode(
        conn,
        V,
        make_episode(
            prompt_id="p-1",
            started_at="2026-09-01T02:00:00+00:00",
            ended_at="2026-09-01T02:05:00Z",
        ),
    )
    records.insert_episode(
        conn, V, make_episode(prompt_id="p-2", started_at=None, ended_at=None)
    )
    [first, second], _ = records.list_episodes(conn, V)
    assert first.started_at is None  # NULL 排前面
    assert second.started_at == "2026-09-01T02:00:00.000Z"
    assert second.ended_at == "2026-09-01T02:05:00.000Z"
    assert [
        e.prompt_id
        for e in records.list_episodes(conn, V, since="2026-09-01T01:00:00Z")[0]
    ] == ["p-1"]


@pytest.mark.parametrize(
    ("scope", "state"),
    [(MISSING, "missing"), (None, "global"), ("Repo", "repo")],
    ids=["missing", "none", "repo"],
)
def test_concept_scope_three_states(conn, vault, scope, state):
    concept = Concept(id="c-1", statement="s", kind="project-fact", scope=scope)
    records.upsert_concept(conn, V, concept)
    [got] = records.get_concepts(conn, V, ["c-1"])
    assert got == concept
    if scope is MISSING:
        assert got.scope is MISSING
    row = conn.execute("SELECT scope_state, scope FROM concepts").fetchone()
    assert row["scope_state"] == state
    assert row["scope"] == (scope if state == "repo" else None)


def test_concept_upsert_overwrites_but_cannot_change_vault(conn, vault, add_vault):
    records.upsert_concept(conn, V, Concept(id="c-1", statement="v1", kind=None))
    records.upsert_concept(
        conn, V, Concept(id="c-1", statement="v2", kind=None, surprisal=0.5)
    )
    [got], _ = records.list_concepts(conn, V)
    assert (got.statement, got.surprisal) == ("v2", 0.5)
    other = add_vault("folder/other")
    with pytest.raises(DuplicateRecord):
        records.upsert_concept(conn, other, Concept(id="c-1", statement="x", kind=None))


def test_injection_round_trip(conn, vault):
    a = Injection(session_id="s-1", prompt_id="p-1", injected=["c-1"])
    b = Injection(
        session_id="s-2", prompt_id=None, injected=[], prompt_fingerprint="fp"
    )
    records.insert_injection(conn, V, a)
    records.insert_injection(conn, V, b, recorded="2026-09-01T00:00:00+00:00")
    assert records.list_injections(conn, V) == ([a, b], None)
    assert records.list_injections(conn, V, session_id="s-2") == ([b], None)
    rec = conn.execute("SELECT recorded FROM injections ORDER BY seq").fetchall()
    assert rec[1][0] == "2026-09-01T00:00:00.000Z"


# ── 分頁：預設上限截斷時呼叫端必須看得到 ─────────────────────────────


def _drain(fetch):
    """沿著 cursor 取完所有頁；回傳 (全部項目, 頁數)。"""
    items, cursor = fetch(None)
    pages = 1
    while cursor is not None:
        more, cursor = fetch(cursor)
        items += more
        pages += 1
    return items, pages


def test_episode_pagination_reports_truncation_across_null_started_at(
    conn, vault, make_episode
):
    # started_at 為 NULL 的列跨在頁界上：tuple 比較遇 NULL 會靜默丟列
    for i, started in enumerate([None, None, "2026-09-01T02:00:00Z", None]):
        records.insert_episode(
            conn,
            V,
            make_episode(prompt_id=f"p-{i}", started_at=started, ended_at=None),
        )
    page, cursor = records.list_episodes(conn, V, limit=2)
    assert len(page) == 2 and cursor is not None
    everything, pages = _drain(
        lambda c: records.list_episodes(conn, V, limit=2, cursor=c)
    )
    assert pages == 2
    assert [e.prompt_id for e in everything] == ["p-0", "p-1", "p-3", "p-2"]
    assert records.list_episodes(conn, V, limit=4)[1] is None


def test_concept_and_injection_pagination_reports_truncation(conn, vault):
    for i in range(5):
        records.upsert_concept(conn, V, Concept(id=f"c-{i}", statement="s", kind=None))
        records.insert_injection(
            conn, V, Injection(session_id=f"s-{i}", prompt_id="p", injected=[])
        )
    page, cursor = records.list_concepts(conn, V, limit=3)
    assert len(page) == 3 and cursor == "c-2"
    concepts, _ = _drain(lambda c: records.list_concepts(conn, V, limit=3, cursor=c))
    assert [c.id for c in concepts] == [f"c-{i}" for i in range(5)]
    page, cursor = records.list_injections(conn, V, limit=3)
    assert len(page) == 3 and cursor is not None
    injections, _ = _drain(
        lambda c: records.list_injections(conn, V, limit=3, cursor=c)
    )
    assert [i.session_id for i in injections] == [f"s-{i}" for i in range(5)]
    assert records.list_concepts(conn, V, limit=5)[1] is None
    assert records.list_injections(conn, V, limit=5)[1] is None


def test_truncation_hint_is_load_bearing(conn, vault, make_episode, monkeypatch):
    """把「多抓一筆判斷下一頁」拿掉（只抓 limit 筆），截斷提示必須消失——
    上面兩個分頁測試就是靠這個提示才會綠，拿掉即紅。"""
    for i in range(3):
        records.insert_episode(conn, V, make_episode(prompt_id=f"p-{i}"))
        records.upsert_concept(conn, V, Concept(id=f"c-{i}", statement="s", kind=None))
    original = conn.execute

    class NoLookahead:
        def execute(self, sql, params=()):
            if "LIMIT ?" in sql:
                params = (*params[:-1], params[-1] - 1)
            return original(sql, params)

    broken = NoLookahead()
    assert records.list_episodes(broken, V, limit=2)[1] is None
    assert records.list_concepts(broken, V, limit=2)[1] is None
    assert records.list_episodes(conn, V, limit=2)[1] is not None
    assert records.list_concepts(conn, V, limit=2)[1] is not None
