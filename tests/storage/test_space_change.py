"""A20：換 space 只允許 lore↔personal，並在單一交易內把 key 改成新前綴。

- 所有引用該 key 的欄位（notes、別名、墓碑、匯入對帳、episodes 等）一起改寫，
  FTS／向量靠 notes.seq 跟著走，改完在新 space 查得到
- 新 key／新別名衝突、別名換不了前綴、殘留資料 → 拒絕
- 中途失敗整段 rollback
- `test_missed_table_is_caught_*`：拿掉某張表的改寫，核對必須抓到並 rollback
- 引用欄位由 schema 動態偵測：新表（欄名 vault）自動納入；偵測失效則拒絕
"""

from __future__ import annotations

import pytest

from lore_vault.schema import Note, Vault
from lore_vault.storage import admin, checks, fts, imports, notes, vectors
from lore_vault.storage.errors import (
    SpaceKeyPrefixRequired,
    StorageError,
    UnknownVault,
    VaultConflict,
)
from lore_vault.storage.vaults import get_vault, resolve_read, upsert_vault

DIM = 4
TS = "2026-09-01T00:00:00.000Z"
OLD = "lore/arc"
NEW = "personal/arc"
SOURCE = "open_notebook"


def _note(vault: str, note_id: str, title: str) -> Note:
    return Note(
        id=note_id, vault=vault, title=title, body="世界觀 祕密", created=TS, updated=TS
    )


def _snapshot(conn) -> dict[str, list[tuple]]:
    """所有引用欄位＋vaults／別名的內容（比對 rollback 用）。"""
    out = {
        "vaults": [tuple(r) for r in conn.execute("SELECT * FROM vaults ORDER BY key")],
        "vault_aliases": [
            tuple(r) for r in conn.execute("SELECT * FROM vault_aliases ORDER BY alias")
        ],
    }
    for table, column in admin.vault_reference_columns(conn):
        out[f"{table}.{column}"] = [
            tuple(r)
            for r in conn.execute(f"SELECT {column} FROM {table} ORDER BY rowid")
        ]
    return out


@pytest.fixture
def lore(conn):
    """lore/arc：2 則 note（FTS、向量、補算）、別名、1 則墓碑、匯入清單、
    episode／concept／injection 各一；另有 dev vault 當對照。"""
    upsert_vault(conn, Vault(key="folder/dev", display="dev"))
    upsert_vault(
        conn, Vault(key=OLD, display="arc", space="lore", aliases=("lore/arc-old",))
    )
    for note_id in ("l-1", "l-2", "l-3"):
        notes.insert_note(
            conn, OLD, _note(OLD, note_id, f"標題 {note_id}"), space="lore"
        )
        vectors.set_embedding(conn, OLD, note_id, [1, 0, 0, 0], space="lore", dim=DIM)
    notes.insert_note(
        conn, "folder/dev", _note("folder/dev", "d-1", "開發"), space="dev"
    )
    entries = [
        imports.ManifestEntry(
            source_id=note_id,
            note_id=note_id,
            vault=OLD,
            content_sha256=imports.content_sha256(f"標題 {note_id}", "世界觀 祕密"),
            source_updated=TS,
        )
        for note_id in ("l-1", "l-3")
    ]
    imports.record_manifest(conn, SOURCE, entries, {OLD: 2})
    for note_id in ("l-1", "l-3"):
        imports.mark_imported(conn, SOURCE, note_id, TS)
    admin.delete_note(conn, OLD, "l-3", space="lore")  # 墓碑
    conn.execute(
        "INSERT INTO episodes (vault, session_id, prompt_id, turn_index, machine, "
        "data, recorded) VALUES (?, 's', 'p', 0, 'm', '{}', ?)",
        (OLD, TS),
    )
    conn.execute(
        "INSERT INTO concepts (id, vault, scope_state, data, updated) "
        "VALUES ('c-1', ?, 'missing', '{}', ?)",
        (OLD, TS),
    )
    conn.execute(
        "INSERT INTO injections (vault, session_id, data, recorded) "
        "VALUES (?, 's', '{}', ?)",
        (OLD, TS),
    )
    return conn


def test_rename_carries_every_relation(lore):
    plan = admin.plan_space_change(lore, OLD, "personal")
    assert plan.new_key == NEW
    assert plan.counts["notes.vault"] == 2
    assert plan.counts["note_tombstones.vault"] == 1
    assert plan.counts["import_sources.vault"] == 2
    assert plan.counts["import_vault_counts.vault"] == 1
    for name in ("episodes.vault", "concepts.vault", "injections.vault"):
        assert plan.counts[name] == 1

    done = admin.change_vault_space(lore, OLD, "personal")
    assert done.counts == plan.counts
    assert not lore.in_transaction

    # note、FTS、向量在新 space／新 key 查得到；舊 space 與舊 key 都查不到
    assert {n.id for n in notes.list_notes(lore, NEW, space="personal")[0]} == {
        "l-1",
        "l-2",
    }
    assert {
        h.note_id for h in fts.search_notes(lore, NEW, "祕密", space="personal")
    } == {
        "l-1",
        "l-2",
    }
    hits = vectors.search_vectors(lore, NEW, [1, 0, 0, 0], space="personal", dim=DIM)
    assert {h.note_id for h in hits} == {"l-1", "l-2"}
    assert fts.search_notes(lore, "*", "祕密", space="lore") == []
    for space in ("lore", "personal"):
        with pytest.raises(UnknownVault):
            resolve_read(lore, OLD, space=space)  # 舊 key 不留別名
    # 別名換前綴並跟到新 key
    assert get_vault(lore, "personal/arc-old", space="personal").key == NEW
    with pytest.raises(UnknownVault):
        resolve_read(lore, "lore/arc-old", space="personal")
    # 墓碑、匯入對帳跟著走：對帳仍綠、墓碑仍擋匯回
    assert admin.find_tombstone(lore, "l-3")["vault"] == NEW
    assert imports.reconcile(lore, SOURCE).status == "pass"
    for table in ("episodes", "concepts", "injections"):
        assert lore.execute(f"SELECT vault FROM {table}").fetchone()[0] == NEW
    # dev 不受影響；space 對帳全綠；外鍵完整
    assert get_vault(lore, "folder/dev", space="dev").key == "folder/dev"
    assert checks.space_valid_values(lore).ok
    assert checks.space_key_prefix_agreement(lore).ok
    assert checks.fts_rows(lore).ok
    assert lore.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_key_option_and_prefix(lore):
    with pytest.raises(SpaceKeyPrefixRequired):
        admin.plan_space_change(lore, OLD, "personal", new_key="lore/other")
    plan = admin.change_vault_space(lore, OLD, "personal", new_key="personal/Diary")
    assert plan.new_key == "personal/diary"
    moved = get_vault(lore, "personal/diary", space="personal")
    assert moved.aliases == ("personal/arc-old",)
    # personal → lore 回得去（lore↔personal 雙向）
    admin.change_vault_space(lore, "personal/diary", "lore")
    assert get_vault(lore, "lore/diary", space="lore").key == "lore/diary"


def test_refuses_dev_and_same_space(lore):
    for key, space in (
        (OLD, "dev"),
        ("folder/dev", "lore"),
        ("folder/dev", "personal"),
    ):
        with pytest.raises(admin.SpaceChangeRefused, match="A20"):
            admin.plan_space_change(lore, key, space)
    with pytest.raises(admin.SpaceChangeRefused):
        admin.plan_space_change(lore, OLD, "lore")


def test_new_key_conflict_refused(lore):
    upsert_vault(
        lore,
        Vault(key=NEW, display="taken", space="personal", aliases=("personal/x",)),
    )
    before = _snapshot(lore)
    with pytest.raises(VaultConflict, match="新 key"):
        admin.change_vault_space(lore, OLD, "personal")
    # 新 key 撞到別的 vault 的別名
    with pytest.raises(VaultConflict, match="別名"):
        admin.change_vault_space(lore, OLD, "personal", new_key="personal/x")
    assert not lore.in_transaction
    assert _snapshot(lore) == before


def test_alias_conflicts_and_unswappable_alias(lore):
    upsert_vault(
        lore,
        Vault(
            key="personal/other",
            display="o",
            space="personal",
            aliases=("personal/arc-old",),
        ),
    )
    with pytest.raises(VaultConflict, match="新別名"):
        admin.plan_space_change(lore, OLD, "personal")
    lore.execute("DELETE FROM vault_aliases WHERE alias = 'personal/arc-old'")
    # 別名不以舊前綴開頭（手動改 DB）→ 換不了前綴就拒絕
    lore.execute("INSERT INTO vault_aliases (alias, vault) VALUES ('arc', ?)", (OLD,))
    with pytest.raises(admin.SpaceChangeRefused, match="別名"):
        admin.plan_space_change(lore, OLD, "personal")


def test_leftover_rows_for_new_key_refused(lore):
    # 墓碑無外鍵：新 key 已有殘留列時不合併
    lore.execute(
        "INSERT INTO note_tombstones (note_id, vault, deleted_at, reason) "
        "VALUES ('ghost', ?, ?, 'x')",
        (NEW, TS),
    )
    with pytest.raises(VaultConflict, match="殘留"):
        admin.plan_space_change(lore, OLD, "personal")


def test_mid_failure_rolls_back(lore, monkeypatch):
    before = _snapshot(lore)
    real = admin._rename_column
    calls = []

    def flaky(conn, table, column, old, new):
        calls.append(table)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return real(conn, table, column, old, new)

    monkeypatch.setattr(admin, "_rename_column", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        admin.change_vault_space(lore, OLD, "personal")
    assert not lore.in_transaction
    assert _snapshot(lore) == before
    assert get_vault(lore, OLD, space="lore").aliases == ("lore/arc-old",)


@pytest.mark.parametrize(
    "skipped", ["note_tombstones", "import_vault_counts", "concepts"]
)
def test_missed_table_is_caught(lore, monkeypatch, skipped):
    """漏改某張表（但謊報筆數）：以資料實況核對必須抓到並 rollback。"""
    before = _snapshot(lore)
    real = admin._rename_column

    def lazy(conn, table, column, old, new):
        if table == skipped:
            sql = f"SELECT count(*) FROM {table} WHERE {column} = ?"
            return conn.execute(sql, (old,)).fetchone()[0]  # 謊報已改
        return real(conn, table, column, old, new)

    monkeypatch.setattr(admin, "_rename_column", lazy)
    with pytest.raises(admin.PlanChanged, match=skipped):
        admin.change_vault_space(lore, OLD, "personal")
    assert not lore.in_transaction
    assert _snapshot(lore) == before


def test_missed_table_guard_is_load_bearing(lore, monkeypatch):
    """拿掉核對時，同樣的漏改會真的留下舊 key 的孤兒墓碑（證明上一個測試有意義）。"""
    real = admin._rename_column

    def lazy(conn, table, column, old, new):
        if table == "note_tombstones":
            return conn.execute(
                "SELECT count(*) FROM note_tombstones WHERE vault = ?", (old,)
            ).fetchone()[0]
        return real(conn, table, column, old, new)

    monkeypatch.setattr(admin, "_rename_column", lazy)
    monkeypatch.setattr(admin, "_verify_renamed", lambda conn, plan: None)
    admin.change_vault_space(lore, OLD, "personal")
    assert admin.find_tombstone(lore, "l-3")["vault"] == OLD


def test_reference_detection_covers_known_and_new_tables(lore):
    found = set(admin.vault_reference_columns(lore))
    present = {r[0] for r in lore.execute("SELECT name FROM sqlite_master")}
    assert {r for r in admin.KNOWN_VAULT_REFERENCES if r[0] in present} <= found
    # 日後新增的表（欄名 vault 或外鍵指向 vaults）自動納入改寫
    lore.execute("CREATE TABLE future_things (id INTEGER PRIMARY KEY, vault TEXT)")
    lore.execute(
        "CREATE TABLE future_refs (id INTEGER PRIMARY KEY, "
        "owner TEXT REFERENCES vaults(key))"
    )
    lore.execute("INSERT INTO future_things (vault) VALUES (?)", (OLD,))
    lore.execute("INSERT INTO future_refs (owner) VALUES (?)", (OLD,))
    plan = admin.change_vault_space(lore, OLD, "personal")
    assert plan.counts["future_things.vault"] == 1
    assert plan.counts["future_refs.owner"] == 1
    assert lore.execute("SELECT vault FROM future_things").fetchone()[0] == NEW
    assert lore.execute("SELECT owner FROM future_refs").fetchone()[0] == NEW


def test_detection_failure_refuses(lore, monkeypatch):
    real = admin.vault_reference_columns
    monkeypatch.setattr(
        admin,
        "vault_reference_columns",
        lambda conn: tuple(r for r in real(conn) if r[0] != "note_tombstones"),
    )
    with pytest.raises(StorageError, match="偵測不完整"):
        admin.plan_space_change(lore, OLD, "personal")
