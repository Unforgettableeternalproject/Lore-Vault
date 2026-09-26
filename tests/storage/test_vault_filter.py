"""T-16：vault 硬過濾。

- 未傳／空值拋錯，不會變成全域查詢；寫入不可用 "*"
- 洩漏測試：先繞過 API 直接查 DB，確認資料確實分 vault 儲存；再驗證正常 API 不洩漏
- `test_leak_test_is_load_bearing`：把過濾條件換成恆真，洩漏測試必須紅
"""

from __future__ import annotations

import pytest

from lore_vault.schema import Concept, Injection, Note
from lore_vault.storage import fts, notes, records, vectors
from lore_vault.storage.errors import UnknownVault, VaultConflict, VaultRequired
from lore_vault.storage.vaults import ALL_VAULTS, get_vault, list_vaults, upsert_vault

DIM = 4
BAD_VAULTS = [None, "", "   ", 123, " folder/a"]


def _readers():
    return {
        "get_notes": lambda c, v: notes.get_notes(c, v, ["a-1"], space="dev"),
        "list_notes": lambda c, v: notes.list_notes(c, v, space="dev"),
        "count_notes": lambda c, v: notes.count_notes(c, v, space="dev"),
        "search_notes": lambda c, v: fts.search_notes(c, v, "共同", space="dev"),
        "search_vectors": lambda c, v: vectors.search_vectors(
            c, v, [1, 0, 0, 0], space="dev", dim=DIM
        ),
        "get_embedding": lambda c, v: vectors.get_embedding(c, v, "a-1", space="dev"),
        "count_without_vector": lambda c, v: vectors.count_without_vector(
            c, v, space="dev", dim=DIM
        ),
        "list_episodes": lambda c, v: records.list_episodes(c, v),
        "count_episodes": lambda c, v: records.count_episodes(c, v),
        "list_concepts": lambda c, v: records.list_concepts(c, v),
        "get_concepts": lambda c, v: records.get_concepts(c, v, ["c-a"]),
        "list_injections": lambda c, v: records.list_injections(c, v),
    }


def _writers():
    note = Note(
        id="w-1",
        vault="folder/a",
        title="t",
        body="",
        created="2026-09-01T00:00:00Z",
        updated="2026-09-01T00:00:00Z",
    )
    return {
        "insert_note": lambda c, v: notes.insert_note(c, v, note, space="dev"),
        "update_note_if": lambda c, v: notes.update_note_if(
            c, v, "a-1", "x", {}, space="dev"
        ),
        "delete_note": lambda c, v: notes.delete_note(c, v, "a-1", space="dev"),
        "set_embedding": lambda c, v: vectors.set_embedding(
            c, v, "a-1", [1, 0, 0, 0], space="dev", dim=DIM
        ),
        "upsert_concept": lambda c, v: records.upsert_concept(
            c, v, Concept(id="c-x", statement="s", kind=None)
        ),
        "insert_injection": lambda c, v: records.insert_injection(
            c, v, Injection(session_id="s", prompt_id="p", injected=[])
        ),
    }


@pytest.fixture
def two_vaults(conn, add_vault, add_note, make_episode):
    add_vault("folder/a")
    add_vault("folder/b")
    add_note("folder/a", "a-1", "A 的筆記", "共同 關鍵字 alpha")
    add_note("folder/b", "b-1", "B 的筆記", "共同 關鍵字 beta")
    vectors.set_embedding(conn, "folder/a", "a-1", [1, 0, 0, 0], space="dev", dim=DIM)
    vectors.set_embedding(conn, "folder/b", "b-1", [1, 0.1, 0, 0], space="dev", dim=DIM)
    records.upsert_concept(
        conn, "folder/a", Concept(id="c-a", statement="a", kind=None)
    )
    records.upsert_concept(
        conn, "folder/b", Concept(id="c-b", statement="b", kind=None)
    )
    for v, sid in (("folder/a", "s-a"), ("folder/b", "s-b")):
        records.insert_injection(
            conn, v, Injection(session_id=sid, prompt_id="p", injected=["c"])
        )
        records.insert_episode(conn, v, make_episode(session_id=sid))
    return conn


@pytest.mark.parametrize("name", sorted(_readers()))
@pytest.mark.parametrize("bad", BAD_VAULTS, ids=repr)
def test_readers_reject_missing_vault(two_vaults, name, bad):
    with pytest.raises(VaultRequired):
        _readers()[name](two_vaults, bad)


@pytest.mark.parametrize("name", sorted(_writers()))
@pytest.mark.parametrize("bad", [*BAD_VAULTS, ALL_VAULTS], ids=repr)
def test_writers_reject_missing_or_wildcard_vault(two_vaults, name, bad):
    with pytest.raises(VaultRequired):
        _writers()[name](two_vaults, bad)


@pytest.mark.parametrize("name", sorted(_readers()))
def test_unknown_vault_raises_not_empty(two_vaults, name):
    with pytest.raises(UnknownVault):
        _readers()[name](two_vaults, "folder/typo")


def test_key_is_case_insensitive_and_aliases_resolve(two_vaults, add_vault):
    add_vault("github.com/me/new", aliases=("github.com/me/old",))
    assert notes.count_notes(two_vaults, "FOLDER/A", space="dev") == 1
    assert (
        get_vault(two_vaults, "GitHub.com/Me/Old", space="dev").key
        == "github.com/me/new"
    )


def test_alias_conflicts_are_rejected(two_vaults, add_vault):
    from lore_vault.schema import Vault

    with pytest.raises(VaultConflict):
        upsert_vault(
            two_vaults, Vault(key="folder/c", display="c", aliases=("folder/a",))
        )
    add_vault("folder/d", aliases=("folder/old",))
    with pytest.raises(VaultConflict):
        upsert_vault(
            two_vaults, Vault(key="folder/e", display="e", aliases=("folder/old",))
        )
    with pytest.raises(VaultConflict):
        upsert_vault(two_vaults, Vault(key="folder/old", display="x"))
    assert [v.key for v in list_vaults(two_vaults, space=None)] == [
        "folder/a",
        "folder/b",
        "folder/d",
    ]


def test_note_vault_must_match_argument(two_vaults):
    note = Note(
        id="x",
        vault="folder/b",
        title="t",
        body="",
        created="2026-09-01T00:00:00Z",
        updated="2026-09-01T00:00:00Z",
    )
    with pytest.raises(VaultRequired, match="不一致"):
        notes.insert_note(two_vaults, "folder/a", note, space="dev")


def test_cross_vault_write_by_id_is_not_found(two_vaults):
    from lore_vault.storage.errors import NotFound

    with pytest.raises(NotFound):
        notes.update_note_if(
            two_vaults, "folder/a", "b-1", "2026-09-01T00:00:00.000Z", {}, space="dev"
        )
    with pytest.raises(NotFound):
        vectors.set_embedding(
            two_vaults, "folder/a", "b-1", [1, 0, 0, 0], space="dev", dim=DIM
        )
    with pytest.raises(NotFound):
        notes.delete_note(two_vaults, "folder/a", "b-1", space="dev")


# ── 洩漏測試 ────────────────────────────────────────────────────────


def _leaks(conn) -> list[str]:
    """以 vault A 的身分走每一條讀取 API，回傳看到 B 資料的 API 名稱。"""
    found = []
    if any(
        n.vault != "folder/a"
        for n in notes.get_notes(conn, "folder/a", ["a-1", "b-1"], space="dev")
    ):
        found.append("get_notes")
    if any(
        n.vault != "folder/a"
        for n in notes.list_notes(conn, "folder/a", space="dev")[0]
    ):
        found.append("list_notes")
    if notes.count_notes(conn, "folder/a", space="dev") != 1:
        found.append("count_notes")
    if any(
        h.vault != "folder/a"
        for h in fts.search_notes(conn, "folder/a", "共同", space="dev")
    ):
        found.append("search_notes")
    hits = vectors.search_vectors(conn, "folder/a", [1, 0, 0, 0], space="dev", dim=DIM)
    if any(h.vault != "folder/a" for h in hits):
        found.append("search_vectors")
    if vectors.get_embedding(conn, "folder/a", "b-1", space="dev") is not None:
        found.append("get_embedding")
    if [c.id for c in records.list_concepts(conn, "folder/a")[0]] != ["c-a"]:
        found.append("list_concepts")
    if records.get_concepts(conn, "folder/a", ["c-b"]):
        found.append("get_concepts")
    if [i.session_id for i in records.list_injections(conn, "folder/a")[0]] != ["s-a"]:
        found.append("list_injections")
    if [e.session_id for e in records.list_episodes(conn, "folder/a")[0]] != ["s-a"]:
        found.append("list_episodes")
    if records.count_episodes(conn, "folder/a") != 1:
        found.append("count_episodes")
    return found


def test_data_is_physically_partitioned_by_vault(two_vaults):
    """繞過過濾層直接查 DB：兩個 vault 的資料都在、而且各自標了 vault。"""
    rows = two_vaults.execute("SELECT id, vault FROM notes ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("a-1", "folder/a"), ("b-1", "folder/b")]
    rows = two_vaults.execute("SELECT id, vault FROM concepts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("c-a", "folder/a"), ("c-b", "folder/b")]
    # FTS 本身不分 vault：MATCH 兩筆都命中，過濾只能靠 API
    raw = two_vaults.execute(
        "SELECT count(*) FROM note_fts WHERE note_fts MATCH ?", ('"共同"',)
    ).fetchone()[0]
    assert raw == 2


def test_normal_api_does_not_leak(two_vaults):
    assert _leaks(two_vaults) == []


def test_explicit_wildcard_reads_across_vaults(two_vaults):
    assert notes.count_notes(two_vaults, ALL_VAULTS, space="dev") == 2
    hits = fts.search_notes(two_vaults, ALL_VAULTS, "共同", space="dev")
    assert {h.vault for h in hits} == {"folder/a", "folder/b"}
    hits = vectors.search_vectors(
        two_vaults, ALL_VAULTS, [1, 0, 0, 0], space="dev", dim=DIM
    )
    assert {h.note_id for h in hits} == {"a-1", "b-1"}


def test_leak_test_is_load_bearing(two_vaults, monkeypatch):
    """把 vault 條件換成恆真（等於拿掉過濾），洩漏測試必須抓到每一條讀取 API。"""

    def no_filter(scope, column):
        return "1 = 1", ()

    for module in (notes, fts, vectors, records):
        monkeypatch.setattr(module, "vault_clause", no_filter)
    assert set(_leaks(two_vaults)) == {
        "get_notes",
        "list_notes",
        "count_notes",
        "search_notes",
        "search_vectors",
        "get_embedding",
        "list_concepts",
        "get_concepts",
        "list_injections",
        "list_episodes",
        "count_episodes",
    }
