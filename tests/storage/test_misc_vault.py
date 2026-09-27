"""D14 雜項 vault（儲存層）：收料路由、保留規則、v16 遷移、doctor 對帳、降權。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lore_vault.recall import service as recall_service
from lore_vault.schema import Concept, Vault
from lore_vault.storage import ingest_checks, manage, records
from lore_vault.storage.errors import ReservedVault, VaultConflict
from lore_vault.storage.migrate import (
    MIGRATIONS,
    SCHEMA_VERSION,
    absorb_folder_vaults,
    current_version,
    migrate,
)
from lore_vault.storage.vaults import (
    KIND_MISC,
    MISC_VAULT_KEY,
    ORIGIN_EPISODE,
    ensure_vault,
    misc_vault_keys,
    route_episode_vault,
    upsert_vault,
)

TS = "2026-09-01T00:00:00.000Z"
REPO = "github.com/o/r"


def _route(conn, key: str):
    return route_episode_vault(conn, key, display=key, origin_detail="{}")


def _insert(conn, key: str, make_episode, *, turn: int = 0) -> None:
    route = _route(conn, key)
    records.insert_episode(
        conn,
        route.key,
        make_episode(session_id=f"s-{key}", turn_index=turn),
        origin_key=route.origin_key,
    )


def _misc_check(conn) -> ingest_checks.Reconciliation:
    return ingest_checks.misc_routing(conn)


# ── 收料路由 ─────────────────────────────────────────────────────────


def test_unregistered_folder_goes_to_misc_created_once(conn):
    first = _route(conn, "Folder/Desktop")
    assert (first.key, first.origin_key, first.created) == (
        MISC_VAULT_KEY,
        "folder/desktop",
        True,
    )
    second = _route(conn, "folder/other")
    assert (second.key, second.origin_key, second.created) == (
        MISC_VAULT_KEY,
        "folder/other",
        False,
    )
    row = conn.execute(
        "SELECT kind, space, origin FROM vaults WHERE key = ?", (MISC_VAULT_KEY,)
    ).fetchone()
    assert tuple(row) == (KIND_MISC, "dev", ORIGIN_EPISODE)
    # folder key 本身沒有被建立
    assert (
        conn.execute(
            "SELECT count(*) FROM vaults WHERE key LIKE 'folder/%'"
        ).fetchone()[0]
        == 0
    )


def test_registered_folder_key_keeps_its_vault(conn, add_vault):
    """`/pm init`（vault_resolve create=true）建立後，同一 folder key 進正式 vault。"""
    add_vault("folder/proj")
    route = _route(conn, "folder/proj")
    assert (route.key, route.origin_key, route.created) == ("folder/proj", None, False)


def test_alias_hit_and_remote_key_behave_as_before(conn, add_vault):
    add_vault("github.com/o/new", aliases=("folder/legacy",))
    assert _route(conn, "folder/legacy").key == "github.com/o/new"
    route = _route(conn, REPO)
    assert (route.key, route.origin_key, route.created) == (REPO, None, True)
    kind = conn.execute("SELECT kind FROM vaults WHERE key = ?", (REPO,)).fetchone()
    assert kind[0] == "repo"


def test_client_cannot_target_misc_directly(conn):
    with pytest.raises(ReservedVault):
        _route(conn, "MISC")


def test_misc_key_taken_by_other_kind_is_refused(conn):
    conn.execute(
        "INSERT INTO vaults (key, display, kind, created) "
        "VALUES ('misc', 'm', 'repo', ?)",
        (TS,),
    )
    with pytest.raises(VaultConflict):
        _route(conn, "folder/x")
    assert _misc_check(conn).status == "fail"


def test_duplicate_resend_after_routing_is_idempotent(conn, make_episode):
    _insert(conn, "folder/a", make_episode)
    route = _route(conn, "folder/a")
    again = records.insert_episode(
        conn,
        route.key,
        make_episode(session_id="s-folder/a", turn_index=0),
        origin_key=route.origin_key,
    )
    assert again is False


# ── 保留規則 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "vault",
    [
        Vault(key="misc", display="m", kind="repo"),
        Vault(key="folder/x", display="x", kind=KIND_MISC),
        Vault(key="folder/x", display="x", aliases=("misc",)),
        Vault(key="misc", display="m", kind=KIND_MISC, aliases=("folder/y",)),
    ],
)
def test_reserved_rules_on_upsert(conn, vault):
    with pytest.raises(ReservedVault):
        upsert_vault(conn, vault)


def test_alias_add_refuses_misc(conn, add_vault):
    add_vault("folder/p")
    _route(conn, "folder/q")
    with pytest.raises(ReservedVault):
        manage.add_alias(conn, "folder/p", "misc", space="dev")
    with pytest.raises(ReservedVault):
        manage.add_alias(conn, MISC_VAULT_KEY, "folder/q", space="dev")


# ── v16 遷移 ────────────────────────────────────────────────────────


def _v15_db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn, migrations=MIGRATIONS[:15])
    return conn


def _raw_vault(conn, key: str, origin: str = "episode") -> None:
    conn.execute(
        "INSERT INTO vaults (key, display, kind, created, origin, origin_detail) "
        "VALUES (?, ?, 'repo', ?, ?, '{}')",
        (key, key, TS, origin),
    )


def _raw_episodes(conn, key: str, n: int) -> None:
    for i in range(n):
        data = json.dumps({"session_id": f"s-{key}", "turn_index": i})
        conn.execute(
            """
            INSERT INTO episodes (vault, session_id, prompt_id, turn_index, machine,
                                  repo, started_at, ended_at, data, recorded)
            VALUES (?, ?, 'p', ?, 'm', 'Desktop', ?, ?, ?, ?)
            """,
            (key, f"s-{key}", i, TS, TS, data, TS),
        )


def test_v16_absorbs_folder_desktop_and_is_idempotent(tmp_path):
    conn = _v15_db(tmp_path)
    try:
        _raw_vault(conn, "folder/desktop")
        _raw_episodes(conn, "folder/desktop", 20)
        # 有 note 的 folder vault：完全不動
        _raw_vault(conn, "folder/withnote")
        _raw_episodes(conn, "folder/withnote", 2)
        conn.execute(
            "INSERT INTO notes (id, vault, title, body, created, updated) "
            "VALUES ('n-1', 'folder/withnote', 't', 'b', ?, ?)",
            (TS, TS),
        )
        # 有 concept 的 folder vault：episode 搬、vault 保留（A7 不搬 concept）
        _raw_vault(conn, "folder/withconcept")
        _raw_episodes(conn, "folder/withconcept", 1)
        conn.execute(
            "INSERT INTO concepts (id, vault, kind, scope_state, scope, data, updated)"
            " VALUES ('c-1', 'folder/withconcept', NULL, 'repo', 'X', '{}', ?)",
            (TS,),
        )
        # 明確建立的 folder vault（/pm init）：不是候選
        _raw_vault(conn, "folder/manual", origin="manual")
        _raw_episodes(conn, "folder/manual", 1)
        before = [tuple(r) for r in conn.execute("SELECT seq, data FROM episodes")]

        migrate(conn)
        assert current_version(conn) == SCHEMA_VERSION == 16

        by_vault = dict(
            conn.execute("SELECT vault, count(*) FROM episodes GROUP BY vault")
        )
        assert by_vault == {
            "misc": 21,
            "folder/withnote": 2,
            "folder/manual": 1,
        }
        origin = dict(
            conn.execute(
                "SELECT origin_key, count(*) FROM episodes WHERE vault = 'misc' "
                "GROUP BY origin_key"
            )
        )
        assert origin == {"folder/desktop": 20, "folder/withconcept": 1}
        assert (
            conn.execute(
                "SELECT count(*) FROM episodes WHERE vault != 'misc' "
                "AND origin_key IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        # data 等凍結欄位不動
        after = [tuple(r) for r in conn.execute("SELECT seq, data FROM episodes")]
        assert after == before
        keys = {r[0] for r in conn.execute("SELECT key FROM vaults")}
        assert "folder/desktop" not in keys
        assert {"folder/withnote", "folder/withconcept", "folder/manual"} <= keys
        detail = json.loads(
            conn.execute(
                "SELECT origin_detail FROM vaults WHERE key = 'misc'"
            ).fetchone()[0]
        )
        actions = {r["key"]: r["action"] for r in detail["migrated_from"]}
        assert actions == {
            "folder/desktop": "moved_and_removed",
            "folder/withconcept": "moved",
        }

        # 冪等：再跑一次只剩被跳過的，資料與紀錄都不變
        def state() -> tuple[list[tuple], list[tuple]]:
            return (
                [
                    tuple(r)
                    for r in conn.execute(
                        "SELECT seq, vault, origin_key FROM episodes ORDER BY seq"
                    )
                ],
                [
                    tuple(r)
                    for r in conn.execute(
                        "SELECT key, kind, origin_detail FROM vaults ORDER BY key"
                    )
                ],
            )

        snapshot = state()
        conn.execute("BEGIN IMMEDIATE")
        again = absorb_folder_vaults(conn)
        conn.execute("COMMIT")
        assert [(r["key"], r["action"]) for r in again] == [
            ("folder/withconcept", "skipped_nothing_to_move"),
            ("folder/withnote", "skipped_has_content"),
        ]
        assert state() == snapshot
        # 留下的收料 folder vault（有 note／concept）由 doctor 以 warn 呈現
        result = _misc_check(conn)
        assert result.status == "warn"
        assert result.counts["misc_episodes"] == 21
    finally:
        conn.close()


def test_v16_without_folder_vaults_creates_nothing(tmp_path):
    conn = _v15_db(tmp_path)
    try:
        _raw_vault(conn, REPO)
        migrate(conn)
        assert misc_vault_keys(conn, space="dev") == frozenset()
        assert _misc_check(conn).status == "pass"
    finally:
        conn.close()


def test_v16_relaxes_kind_check_but_still_enforces_it(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('x', 'x', 'bad', ?)",
            (TS,),
        )
    conn.execute(
        "INSERT INTO vaults (key, display, kind, created) "
        "VALUES ('misc', 'm', 'misc', ?)",
        (TS,),
    )
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ── doctor vaults.misc_routing ─────────────────────────────────────


def test_misc_routing_passes_on_routed_data(conn, make_episode, add_vault):
    add_vault(REPO)
    _insert(conn, "folder/a", make_episode)
    _insert(conn, REPO, make_episode)
    result = _misc_check(conn)
    assert result.status == "pass", result
    assert result.counts["misc_episodes"] == 1


def test_misc_routing_fails_when_origin_key_lost(conn, make_episode):
    _insert(conn, "folder/a", make_episode)
    conn.execute("UPDATE episodes SET origin_key = NULL")
    assert _misc_check(conn).status == "fail"


def test_misc_routing_fails_when_origin_key_on_regular_vault(
    conn, make_episode, add_vault
):
    add_vault(REPO)
    _insert(conn, REPO, make_episode)
    conn.execute("UPDATE episodes SET origin_key = 'folder/x'")
    assert _misc_check(conn).status == "fail"


def test_misc_routing_fails_on_alias_to_misc(conn):
    _route(conn, "folder/a")
    conn.execute("INSERT INTO vault_aliases (alias, vault) VALUES ('folder/a', 'misc')")
    assert _misc_check(conn).status == "fail"


def test_misc_routing_warns_when_folder_vault_auto_created(conn):
    """拿掉路由（舊行為：folder key 自動建 vault）時 doctor 會亮。"""
    assert _misc_check(conn).status == "pass"
    ensure_vault(
        conn,
        Vault(key="folder/x", display="x", kind="repo"),
        origin=ORIGIN_EPISODE,
    )
    assert _misc_check(conn).status == "warn"


def test_misc_routing_reports_registered_origins(conn, make_episode, add_vault):
    _insert(conn, "folder/a", make_episode)
    add_vault("folder/a")  # 事後 /pm init：舊 episode 留在雜項（A7），只列資訊
    result = _misc_check(conn)
    assert result.status == "pass"
    assert result.counts["registered_origins"] == 1


# ── 低優先度：注入匯出順序、recall 降權 ─────────────────────────────


def _concept(cid: str, scope: str) -> Concept:
    return Concept.from_dict(
        {"id": cid, "statement": f"s {cid}", "kind": "project-fact", "scope": scope}
    )


def test_export_puts_misc_concepts_last(conn, add_vault):
    add_vault(REPO)
    misc = _route(conn, "folder/a").key
    records.upsert_concept(conn, misc, _concept("c-misc", "A"))
    records.upsert_concept(conn, REPO, _concept("c-repo", "R"))
    records.upsert_concept(conn, misc, _concept("c-misc-2", "A"))
    concepts, _ = records.export_concepts(conn, "*")
    assert [c.id for c in concepts] == ["c-repo", "c-misc", "c-misc-2"]


def test_recall_all_vaults_demotes_misc(conn, add_vault, add_note, monkeypatch):
    add_vault(REPO)
    misc = _route(conn, "folder/a").key
    # 雜項那則詞頻較高，原始 RRF 會排第一
    add_note(misc, "n-misc", "kestrel kestrel", "kestrel kestrel kestrel")
    add_note(REPO, "n-repo", "notes", "kestrel")

    def ids(vault: str) -> list[str]:
        result = recall_service.recall(
            conn, "kestrel", vault, space="dev", mode="lexical", kinds=["note"]
        )
        return [item.id for item in result.items]

    assert ids("*") == ["n-repo", "n-misc"]
    # 單一 vault 查詢不降權（明示要看雜項）
    assert ids(misc) == ["n-misc"]
    # 拿掉降權即紅：雜項那則回到第一
    monkeypatch.setattr(recall_service, "MISC_SCORE_WEIGHT", 1.0)
    assert ids("*") == ["n-misc", "n-repo"]
