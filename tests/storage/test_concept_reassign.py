"""concept 改歸屬（`storage.admin.reassign_concepts`／`cli.admin reassign-concepts`）。

接在 episode 改歸屬之後：漂移輪次蒸餾出來的 concept 依 A17 跟著來源 episode 進了
雜項 vault，而既有 id 的歸屬是凍結的，episode 搬走後它不會自己跟上。
"""

from __future__ import annotations

import io
import json
import sqlite3

import pytest

from lore_vault.cli import admin as cli
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.schema import Concept, Vault
from lore_vault.storage import admin, records
from lore_vault.storage.errors import DuplicateRecord, UnknownVault
from lore_vault.storage.vaults import (
    MISC_VAULT_KEY,
    ensure_vault,
    route_episode_vault,
)

SESSION = "sess-comfy"
TARGET = "folder/comfyui"
DRIFTED = "folder/character_specs"


def _concept(cid: str, scope: str | None, turns=(("p1", 1),)) -> Concept:
    return Concept.from_dict(
        {
            "id": cid,
            "statement": f"s {cid}",
            "kind": "project-fact",
            "scope": scope,
            "source_turns": [list(t) for t in turns],
        }
    )


def _rows(conn) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT id, vault, scope, ord, data FROM concepts ORDER BY id"
        )
    ]


def _dump(conn) -> dict[str, list[tuple]]:
    return {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("episodes", "concepts", "injections", "vaults")
    }


def _doctor_failures(conn) -> list[str]:
    report = default_registry().run(
        DoctorContext(settings={}, resources={"db": conn}), categories=["vaults"]
    )
    return [o.name for o in report.outcomes if o.result.status is Status.FAIL]


@pytest.fixture
def pool(conn, add_vault, make_episode):
    """c-a（ord 1，雜項）、c-b（ord 2，目標 vault）、c-c（ord 3，雜項）；
    c-a／c-c 的來源輪次 p1#1 還在雜項。"""
    add_vault(TARGET)
    route = route_episode_vault(conn, DRIFTED, display=DRIFTED, origin_detail="{}")
    records.insert_episode(
        conn,
        route.key,
        make_episode(
            session_id=SESSION,
            prompt_id="p1",
            turn_index=1,
            repo="character_specs",
            repo_root=None,
        ),
        origin_key=route.origin_key,
    )
    records.upsert_concept(conn, MISC_VAULT_KEY, _concept("c-a", "character_specs"))
    records.upsert_concept(conn, TARGET, _concept("c-b", "ComfyUI", turns=()))
    records.upsert_concept(conn, MISC_VAULT_KEY, _concept("c-c", "character_specs"))
    return conn


def _export_ids(conn) -> list[str]:
    return [c.id for c in records.export_concepts(conn, "*")[0]]


def test_plan_lists_vault_and_scope_without_writing(pool):
    conn = pool
    before = _dump(conn)
    plan = admin.plan_concept_reassign(conn, ["c-a", "c-b", "c-c"], TARGET)
    assert plan.unchanged == ("c-b",)
    listed = [(m["id"], m["vault"], m["scope"], m["source_vaults"]) for m in plan.moves]
    assert listed == [
        ("c-a", MISC_VAULT_KEY, "character_specs", [MISC_VAULT_KEY]),
        ("c-c", MISC_VAULT_KEY, "character_specs", [MISC_VAULT_KEY]),
    ]
    # 來源 episode 還沒搬：提醒先跑 reassign-episodes
    assert len(plan.warnings) == 2 and "reassign-episodes" in plan.warnings[0]
    assert "statement" not in json.dumps(plan.to_dict())
    assert _dump(conn) == before


def test_no_warning_once_source_episodes_are_moved(pool):
    conn = pool
    admin.reassign_episodes(conn, SESSION, [1], TARGET, repo="ComfyUI")
    assert admin.plan_concept_reassign(conn, ["c-a", "c-c"], TARGET).warnings == ()


def test_reassign_moves_only_the_vault_column(pool):
    conn = pool
    before = _rows(conn)
    # 雜項的 concept 在匯出時排最後
    assert _export_ids(conn) == ["c-b", "c-a", "c-c"]

    plan = admin.reassign_concepts(conn, ["c-a", "c-c"], TARGET)
    assert [m["id"] for m in plan.moves] == ["c-a", "c-c"]
    after = _rows(conn)
    assert [r[1] for r in after] == [TARGET] * 3
    # scope、ord、data 逐欄不變
    assert [(r[0], *r[2:]) for r in after] == [(r[0], *r[2:]) for r in before]
    # 搬出雜項後回到依 ord 的位置
    assert _export_ids(conn) == ["c-a", "c-b", "c-c"]
    assert _doctor_failures(conn) == []
    # 冪等
    again = admin.reassign_concepts(conn, ["c-a", "c-c"], TARGET)
    assert again.moves == () and again.unchanged == ("c-a", "c-c")
    assert _rows(conn) == after


def test_frozen_ownership_is_why_the_tool_exists(pool):
    """既有 id 寫進別的 vault 會被拒（歸屬凍結）；搬過去之後同一筆推送才是更新。"""
    conn = pool
    corrected = _concept("c-a", "ComfyUI")
    with pytest.raises(DuplicateRecord):
        records.upsert_concept(conn, TARGET, corrected)
    admin.reassign_concepts(conn, ["c-a"], TARGET)
    assert records.upsert_concept(conn, TARGET, corrected) == records.CONCEPT_UPDATED
    scope = conn.execute("SELECT scope FROM concepts WHERE id = 'c-a'").fetchone()[0]
    assert scope == "ComfyUI"


def test_refuses_whole_batch_when_any_id_is_missing(pool):
    conn = pool
    before = _dump(conn)
    with pytest.raises(admin.ReassignRefused, match="c-zzz"):
        admin.reassign_concepts(conn, ["c-a", "c-zzz", "c-c"], TARGET)
    with pytest.raises(admin.ReassignRefused):
        admin.reassign_concepts(conn, [], TARGET)
    assert _dump(conn) == before


def test_target_must_be_an_existing_repo_vault(pool):
    conn = pool
    ensure_vault(
        conn, Vault(key="global", display="global", kind="global"), origin="pipeline"
    )
    before = _dump(conn)
    with pytest.raises(UnknownVault):
        admin.reassign_concepts(conn, ["c-a"], "folder/typo")
    for key in (MISC_VAULT_KEY, "global"):
        with pytest.raises(admin.ReassignRefused, match="kind"):
            admin.reassign_concepts(conn, ["c-b"], key)
    assert _dump(conn) == before


def test_unknown_target_would_break_the_foreign_key(pool):
    """拿掉保護會紅：不解析目標就改寫，vault 外鍵當場擋下。"""
    with pytest.raises(sqlite3.IntegrityError):
        pool.execute("UPDATE concepts SET vault = 'folder/typo' WHERE id = 'c-a'")


def test_plan_changed_rolls_back_the_whole_batch(pool, monkeypatch):
    conn = pool
    before = _dump(conn)
    monkeypatch.setattr(admin, "_count", lambda *_args: 1)
    with pytest.raises(admin.PlanChanged, match="核對不符"):
        admin.reassign_concepts(conn, ["c-a", "c-c"], TARGET)
    assert _dump(conn) == before


# ── CLI ──


def _run(db_path, *args) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = cli.main(["--db", str(db_path), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def test_cli_dry_run_then_apply(pool, db_path):
    conn = pool
    args = ("reassign-concepts", "--ids", "c-a, c-c", "--to", TARGET)
    before = _dump(conn)
    code, dry = _run(db_path, *args)
    assert code == 0 and dry["mode"] == "dry_run"
    assert dry["counts"] == {"moves": 2, "unchanged": 0}
    assert [(m["id"], m["vault"], m["scope"]) for m in dry["moves"]] == [
        ("c-a", MISC_VAULT_KEY, "character_specs"),
        ("c-c", MISC_VAULT_KEY, "character_specs"),
    ]
    assert _dump(conn) == before

    code, done = _run(db_path, *args, "--yes")
    assert code == 0 and done["mode"] == "reassigned"
    assert [r[1] for r in _rows(conn)] == [TARGET] * 3


def test_cli_reports_refusal_as_exit_1(pool, db_path, capsys):
    before = _dump(pool)
    code, out = _run(
        db_path, "reassign-concepts", "--ids", "c-a,c-zzz", "--to", TARGET, "--yes"
    )
    assert code == 1 and out is None
    assert "不存在" in capsys.readouterr().err
    assert _dump(pool) == before


def test_refuses_universal_concepts(pool):
    """scope=None 的通用 concept 只屬於 global（A17）；混在清單裡就整批拒絕。"""
    conn = pool
    ensure_vault(
        conn, Vault(key="global", display="global", kind="global"), origin="pipeline"
    )
    records.upsert_concept(conn, "global", _concept("c-g", None))
    before = _dump(conn)
    with pytest.raises(admin.ReassignRefused, match="c-g"):
        admin.reassign_concepts(conn, ["c-a", "c-g"], TARGET)
    assert _dump(conn) == before
