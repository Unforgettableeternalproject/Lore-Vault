"""階段 8 儲存層：v4 遷移、自動建 vault、concept 順序／刪除、注入冪等、doctor 對帳。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.schema import Concept, Injection, Vault
from lore_vault.storage import ingest_checks, records
from lore_vault.storage.errors import DuplicateRecord, NotFound
from lore_vault.storage.migrate import MIGRATIONS, current_version, migrate
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import (
    ORIGIN_EPISODE,
    ORIGIN_MANUAL,
    ensure_vault,
    upsert_vault,
    vault_origins,
)

V = "github.com/o/r"


@pytest.fixture
def vault(add_vault):
    return add_vault(V)


def test_v3_database_upgrades_to_v4_keeping_rows(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn, migrations=MIGRATIONS[:3])
    conn.execute(
        "INSERT INTO vaults (key, display, kind, created) VALUES (?, ?, 'repo', ?)",
        (V, "R", "2026-09-01T00:00:00.000Z"),
    )
    for cid in ("c-b", "c-a"):
        conn.execute(
            """
            INSERT INTO concepts (id, vault, kind, scope_state, scope, data, updated)
            VALUES (?, ?, NULL, 'global', NULL, ?, '2026-09-01T00:00:00.000Z')
            """,
            (
                cid,
                V,
                f'{{"id": "{cid}", "kind": null, "scope": null, "statement": "s"}}',
            ),
        )
    assert current_version(conn) == 3
    migrate(conn)
    assert current_version(conn) == len(MIGRATIONS)
    assert vault_origins(conn) == [(V, ORIGIN_MANUAL, None)]
    # 既有 concept 依寫入順序（rowid）回填 ord，不依 id
    concepts, _ = records.export_concepts(conn, "*")
    assert [c.id for c in concepts] == ["c-b", "c-a"]
    conn.close()


def test_ensure_vault_creates_once_and_upsert_keeps_origin(conn):
    new = Vault(key="Folder/X", display="X")
    assert ensure_vault(conn, new, origin=ORIGIN_EPISODE, origin_detail="{}") == (
        "folder/x",
        True,
    )
    assert ensure_vault(conn, new, origin=ORIGIN_EPISODE) == ("folder/x", False)
    upsert_vault(conn, Vault(key="folder/x", display="改名"))
    assert vault_origins(conn) == [("folder/x", ORIGIN_EPISODE, "{}")]
    with pytest.raises(ValueError):
        ensure_vault(conn, Vault(key="a", display="a"), origin=ORIGIN_MANUAL)


def test_concept_modes_order_and_delete(conn, vault, add_vault):
    other = add_vault("folder/other")
    c = Concept(id="c-2", statement="s", kind=None, scope="R")
    assert records.upsert_concept(conn, V, c) == records.CONCEPT_CREATED
    assert records.upsert_concept(conn, V, c) == records.CONCEPT_UNCHANGED
    with pytest.raises(DuplicateRecord):
        records.upsert_concept(conn, V, c, mode="create")
    with pytest.raises(NotFound):
        records.upsert_concept(
            conn, V, Concept(id="c-9", statement="s", kind=None), mode="update"
        )
    records.upsert_concept(
        conn, V, Concept(id="c-1", statement="s", kind=None, scope=None)
    )
    assert (
        records.upsert_concept(
            conn, V, Concept(id="c-2", statement="改", kind=None, scope="R")
        )
        == records.CONCEPT_UPDATED
    )
    concepts, excluded = records.export_concepts(conn, "*")
    assert [x.id for x in concepts] == ["c-2", "c-1"] and excluded == 0
    with pytest.raises(DuplicateRecord):
        records.delete_concept(conn, other, "c-2")
    assert records.delete_concept(conn, V, "c-2") is True
    assert records.delete_concept(conn, V, "c-2") is False
    # 刪掉後新增的仍排最後（不重用被刪的位置）
    records.upsert_concept(
        conn, V, Concept(id="c-0", statement="s", kind=None, scope="R")
    )
    assert [x.id for x in records.export_concepts(conn, V)[0]] == ["c-1", "c-0"]


def test_injection_insert_is_idempotent(conn, vault):
    a = Injection(session_id="s", prompt_id="p", injected=["c-1"])
    b = Injection(session_id="s", prompt_id=None, injected=[], prompt_fingerprint="f")
    assert records.insert_injection(conn, V, a) is True
    assert (
        records.insert_injection(conn, V, a, recorded="2026-09-02T00:00:00Z") is False
    )
    assert records.insert_injection(conn, V, b) is True
    assert records.insert_injection(conn, V, b) is False
    other = Injection(session_id="s", prompt_id="p", injected=["c-2"])
    assert records.insert_injection(conn, V, other) is True
    assert conn.execute("SELECT count(*) FROM injections").fetchone()[0] == 3


# ── doctor ──────────────────────────────────────────────────────────


def _run(conn, **settings):
    report = default_registry().run(
        DoctorContext(settings=settings, resources={"db": conn}),
        categories=["episodes", "vaults"],
    )
    return {o.name: o.to_dict() for o in report.outcomes}


def _set_recorded(conn, when: datetime) -> None:
    conn.execute("UPDATE episodes SET recorded = ?", (format_utc(when),))


def test_ingest_recency_warns_when_machine_stale(conn, vault, make_episode):
    now = datetime(2026, 9, 26, tzinfo=UTC)
    empty = _run(conn, now=now)["episodes.ingest_recency"]
    assert empty["status"] == "warn"

    records.insert_episode(conn, V, make_episode(machine="a"))
    records.insert_episode(conn, V, make_episode(machine="b", prompt_id="p-2"))
    _set_recorded(conn, now - timedelta(hours=1))
    fresh = _run(conn, now=now)["episodes.ingest_recency"]
    assert fresh["status"] == "pass"
    assert fresh["counts"] == {"episodes": 2, "machines": 2, "stale_machines": 0}

    conn.execute(
        "UPDATE episodes SET recorded = ? WHERE machine = 'b'",
        (format_utc(now - timedelta(hours=49)),),
    )
    stale = _run(conn, now=now)["episodes.ingest_recency"]
    assert stale["status"] == "warn"
    assert stale["counts"]["stale_machines"] == 1
    assert "b" in stale["summary"]
    # 門檻可調
    assert (
        _run(conn, now=now, episode_ingest_max_age_hours=72)["episodes.ingest_recency"][
            "status"
        ]
        == "pass"
    )


def test_auto_created_vaults_counted(conn, vault):
    ensure_vault(
        conn,
        Vault(key="folder/auto", display="a"),
        origin=ORIGIN_EPISODE,
        origin_detail='{"machine": "a"}',
    )
    result = _run(conn)["vaults.auto_created"]
    assert result["status"] == "pass"
    assert result["counts"]["auto_created"] == 1
    assert result["counts"]["origin_manual"] == 1
    assert any("folder/auto" in d for d in result["details"])
    assert (
        _run(conn, auto_vault_warn_above=0)["vaults.auto_created"]["status"] == "warn"
    )


def test_ingest_checks_are_pure(conn, vault):
    rec = ingest_checks.auto_created_vaults(conn)
    assert rec.counts["auto_created"] == 0
