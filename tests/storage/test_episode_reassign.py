"""episode 改歸屬（`storage.admin.reassign_episodes`／`cli.admin reassign-episodes`）。

情境取自 2026-10-09 的實際事故：非 git 專案目錄的 session，部分輪次的 repo 被標成
子目錄名（`site-packages`），收料時 `folder/site-packages` 未註冊而進了雜項 vault，
同一個 session 被拆到 `folder/comfyui` 與 `misc` 兩邊。
"""

from __future__ import annotations

import io
import json

import pytest

from lore_vault.cli import admin as cli
from lore_vault.schema import Concept, Injection
from lore_vault.storage import admin, ingest_checks, records
from lore_vault.storage.errors import DuplicateRecord, UnknownVault
from lore_vault.storage.vaults import (
    MISC_VAULT_KEY,
    route_episode_vault,
    route_injection_vault,
)

SESSION = "sess-comfy"
TARGET = "folder/comfyui"
DRIFTED = "folder/site-packages"


def _ingest(conn, make_episode, key: str, *, session: str = SESSION, **fields):
    episode = make_episode(session_id=session, repo_root=None, **fields)
    route = route_episode_vault(conn, key, display=key, origin_detail="{}")
    records.insert_episode(conn, route.key, episode, origin_key=route.origin_key)
    return episode


def _rows(conn, session: str = SESSION) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT turn_index, vault, origin_key, repo FROM episodes "
            "WHERE session_id = ? ORDER BY turn_index",
            (session,),
        )
    ]


@pytest.fixture
def split_session(conn, add_vault, make_episode):
    """turn 0 在已註冊的 folder/comfyui；turn 1、2 被標成 site-packages 進了雜項。"""
    add_vault(TARGET)
    _ingest(conn, make_episode, TARGET, prompt_id="p0", turn_index=0, repo="ComfyUI")
    for turn in (1, 2):
        _ingest(
            conn,
            make_episode,
            DRIFTED,
            prompt_id=f"p{turn}",
            turn_index=turn,
            repo="site-packages",
        )
    # 別的 session 也從同名位置進過雜項：不在範圍內，不可被波及
    _ingest(
        conn,
        make_episode,
        DRIFTED,
        session="sess-other",
        prompt_id="q1",
        turn_index=1,
        repo="site-packages",
    )
    assert _rows(conn) == [
        (0, TARGET, None, "ComfyUI"),
        (1, MISC_VAULT_KEY, DRIFTED, "site-packages"),
        (2, MISC_VAULT_KEY, DRIFTED, "site-packages"),
    ]
    return conn


def test_plan_lists_moves_without_writing(split_session):
    conn = split_session
    plan = admin.plan_episode_reassign(conn, SESSION, [0, 1, 2], TARGET)
    assert plan.target == TARGET and plan.unchanged == (0,)
    assert [(m["turn_index"], m["vault"], m["origin_key"]) for m in plan.moves] == [
        (1, MISC_VAULT_KEY, DRIFTED),
        (2, MISC_VAULT_KEY, DRIFTED),
    ]
    # 規劃只有鍵與歸屬，不帶語料內容
    assert "user_text" not in json.dumps(plan.to_dict())
    assert _rows(conn)[1][1] == MISC_VAULT_KEY


def test_reassign_moves_vault_and_clears_origin_key(split_session):
    conn = split_session
    plan = admin.reassign_episodes(conn, SESSION, [1, 2], TARGET)
    assert len(plan.moves) == 2
    assert _rows(conn) == [
        (0, TARGET, None, "ComfyUI"),
        (1, TARGET, None, "site-packages"),
        (2, TARGET, None, "site-packages"),
    ]
    assert _rows(conn, "sess-other") == [(1, MISC_VAULT_KEY, DRIFTED, "site-packages")]
    assert ingest_checks.misc_routing(conn).status == "pass"
    # 重跑：全部已到位，不再改寫
    again = admin.reassign_episodes(conn, SESSION, [1, 2], TARGET)
    assert again.moves == () and again.unchanged == (1, 2)


def test_misc_routing_fails_if_origin_key_were_kept(split_session):
    """拿掉保護會紅：只改 vault 不清 origin_key，`vaults.misc_routing` 抓得到。"""
    conn = split_session
    conn.execute(
        "UPDATE episodes SET vault = ? WHERE session_id = ? AND turn_index = 1",
        (TARGET, SESSION),
    )
    check = ingest_checks.misc_routing(conn)
    assert check.status == "fail"
    assert any("origin_key" in line for line in check.details)


def test_repo_option_rewrites_column_and_data(split_session, make_episode):
    """`--repo`：服務端內容與修正後的本機語料一致，重送是 duplicate 不是 conflict。"""
    conn = split_session
    corrected = make_episode(
        session_id=SESSION,
        repo_root=None,
        prompt_id="p1",
        turn_index=1,
        repo="ComfyUI",
    )
    with pytest.raises(DuplicateRecord):
        records.insert_episode(conn, TARGET, corrected)

    admin.reassign_episodes(conn, SESSION, [1, 2], TARGET, repo="ComfyUI")
    assert _rows(conn)[1:] == [
        (1, TARGET, None, "ComfyUI"),
        (2, TARGET, None, "ComfyUI"),
    ]
    stored, _ = records.list_episodes(conn, TARGET)
    assert {e.repo for e in stored} == {"ComfyUI"}
    assert records.insert_episode(conn, TARGET, corrected) is False


def test_refuses_whole_batch_when_any_turn_is_missing(split_session):
    conn = split_session
    before = _rows(conn)
    with pytest.raises(admin.ReassignRefused, match=r"\[7\]"):
        admin.reassign_episodes(conn, SESSION, [1, 2, 7], TARGET)
    with pytest.raises(admin.ReassignRefused):
        admin.reassign_episodes(conn, "no-such-session", [1], TARGET)
    with pytest.raises(admin.ReassignRefused):
        admin.reassign_episodes(conn, SESSION, [], TARGET)
    assert _rows(conn) == before


def test_target_must_exist_and_not_be_misc(split_session):
    conn = split_session
    before = _rows(conn)
    # 未註冊的位置不自動建 vault
    with pytest.raises(UnknownVault):
        admin.reassign_episodes(conn, SESSION, [1], "folder/typo")
    with pytest.raises(admin.ReassignRefused, match="雜項"):
        admin.reassign_episodes(conn, SESSION, [0], MISC_VAULT_KEY)
    assert _rows(conn) == before


def test_related_injections_and_concepts_are_listed_not_moved(split_session):
    conn = split_session
    route = route_injection_vault(conn, DRIFTED)
    records.insert_injection(
        conn,
        route.key,
        Injection.from_dict(
            {"session_id": SESSION, "prompt_id": "p1", "injected": ["c-1"]}
        ),
        origin_key=route.origin_key,
    )
    records.upsert_concept(
        conn,
        MISC_VAULT_KEY,
        Concept.from_dict(
            {
                "id": "c-drift",
                "statement": "s",
                "kind": "project-fact",
                "scope": "site-packages",
                "source_turns": [["p1", 1], ["p0", 0]],
            }
        ),
    )
    plan = admin.reassign_episodes(conn, SESSION, [1, 2], TARGET)
    assert plan.to_dict()["not_moved"] == {
        "injections": [{"vault": MISC_VAULT_KEY, "origin_key": DRIFTED, "count": 1}],
        "concepts": [
            {
                "id": "c-drift",
                "vault": MISC_VAULT_KEY,
                "scope": "site-packages",
                "source_turns": 2,
                "in_range": 1,
            }
        ],
    }
    assert conn.execute("SELECT vault FROM injections").fetchone()[0] == MISC_VAULT_KEY
    assert records.concept_owner(conn, "c-drift") == MISC_VAULT_KEY


# ── CLI ──


def _run(db_path, *args) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = cli.main(["--db", str(db_path), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def test_cli_dry_run_then_apply(split_session, db_path):
    conn = split_session
    args = ("reassign-episodes", "--session", SESSION, "--turns", "1-2", "--to", TARGET)
    code, dry = _run(db_path, *args)
    assert code == 0 and dry["mode"] == "dry_run"
    assert dry["counts"] == {"moves": 2, "unchanged": 0}
    assert _rows(conn)[1][1] == MISC_VAULT_KEY

    code, done = _run(db_path, *args, "--repo", "ComfyUI", "--yes")
    assert code == 0 and done["mode"] == "reassigned"
    assert "episode_cache" in done["hint"]
    assert _rows(conn)[1:] == [
        (1, TARGET, None, "ComfyUI"),
        (2, TARGET, None, "ComfyUI"),
    ]


def test_cli_reports_refusal_as_exit_1(split_session, db_path, capsys):
    code, out = _run(
        db_path,
        "reassign-episodes",
        "--session",
        SESSION,
        "--turns",
        "1,9",
        "--to",
        TARGET,
        "--yes",
    )
    assert code == 1 and out is None
    assert "沒有這些輪次" in capsys.readouterr().err
    assert _rows(split_session)[1][1] == MISC_VAULT_KEY


@pytest.mark.parametrize("value", ["x", "5-3", "-1", "1-"])
def test_cli_rejects_malformed_turns(db_path, conn, value):
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--db",
                str(db_path),
                "reassign-episodes",
                "--session",
                SESSION,
                "--turns",
                value,
                "--to",
                TARGET,
            ]
        )


def _dump(conn) -> dict[str, list[tuple]]:
    """改歸屬會碰到的表的完整內容（逐列逐欄），用來證明零寫入／完整 rollback。"""
    return {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("episodes", "concepts", "injections", "vaults")
    }


def test_cli_dry_run_writes_nothing(split_session, db_path):
    conn = split_session
    before = _dump(conn)
    code, dry = _run(
        db_path,
        "reassign-episodes",
        "--session",
        SESSION,
        "--turns",
        "0-2",
        "--to",
        TARGET,
        "--repo",
        "ComfyUI",
    )
    assert code == 0 and dry["mode"] == "dry_run" and dry["counts"]["moves"] == 2
    assert _dump(conn) == before


def test_plan_changed_rolls_back_the_whole_batch(split_session, monkeypatch):
    """改寫完的核對不符 → `PlanChanged`，已改寫的列全部回到原狀。"""
    conn = split_session
    before = _dump(conn)
    monkeypatch.setattr(admin, "_count", lambda *_args: 1)
    with pytest.raises(admin.PlanChanged, match="核對不符"):
        admin.reassign_episodes(conn, SESSION, [1, 2], TARGET, repo="ComfyUI")
    assert _dump(conn) == before


def test_refuses_when_a_turn_index_matches_several_episodes(
    split_session, make_episode
):
    conn = split_session
    _ingest(
        conn,
        make_episode,
        DRIFTED,
        prompt_id="p1-again",
        turn_index=1,
        repo="site-packages",
    )
    before = _dump(conn)
    with pytest.raises(admin.ReassignRefused, match="多筆"):
        admin.reassign_episodes(conn, SESSION, [1, 2], TARGET)
    assert _dump(conn) == before


def test_warns_when_source_is_not_misc(split_session, add_vault):
    conn = split_session
    add_vault("folder/elsewhere")
    assert admin.plan_episode_reassign(conn, SESSION, [1, 2], TARGET).warnings == ()
    plan = admin.plan_episode_reassign(conn, SESSION, [0, 1], "folder/elsewhere")
    (warning,) = plan.warnings
    assert "[0]" in warning and TARGET in warning and "不在雜項" in warning
    assert plan.to_dict()["warnings"] == [warning]


def test_warns_when_repo_would_not_resolve_to_target(split_session, add_vault):
    """`--repo` 對應的客戶端 key 不是目標：客戶端重送修正後的內容會 conflict。"""
    conn = split_session
    add_vault("folder/elsewhere")
    ok = admin.plan_episode_reassign(conn, SESSION, [1, 2], TARGET, repo="ComfyUI")
    assert ok.warnings == ()
    (other,) = admin.plan_episode_reassign(
        conn, SESSION, [1, 2], TARGET, repo="Elsewhere"
    ).warnings
    assert "folder/elsewhere" in other and "conflict" in other
    (unknown,) = admin.plan_episode_reassign(
        conn, SESSION, [1, 2], TARGET, repo="Nowhere"
    ).warnings
    assert "未註冊" in unknown
