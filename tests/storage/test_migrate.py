"""T-15：從空檔建出完整 schema、WAL 生效、遷移的版本與交易語意。"""

from __future__ import annotations

import sqlite3

import pytest

from lore_vault.storage import migrate as migrate_mod
from lore_vault.storage.db import connect, connect_readonly
from lore_vault.storage.errors import SchemaVersionError, StorageError
from lore_vault.storage.migrate import SCHEMA_VERSION, current_version, migrate

EXPECTED_TABLES = {
    "vaults",
    "vault_aliases",
    "notes",
    "note_fts",
    "note_embeddings",
    "episodes",
    "concepts",
    "injections",
    "note_tombstones",
    "documents",
    "document_chunks",
    "chunk_fts",
    "document_chunk_embeddings",
    "document_tombstones",
    "document_enrichment",
}


def test_fresh_file_gets_full_schema(conn, db_path):
    assert db_path.exists()
    assert current_version(conn) == SCHEMA_VERSION
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert EXPECTED_TABLES <= names


def test_wal_and_foreign_keys_enabled(conn, db_path):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # WAL 是持久設定：另一條連線看到的也是 wal
    other = sqlite3.connect(db_path)
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        other.close()


def test_reopen_is_idempotent(conn, db_path):
    conn.close()
    again = connect(db_path)
    try:
        assert current_version(again) == SCHEMA_VERSION
    finally:
        again.close()


def test_newer_db_version_is_refused(db_path):
    first = connect(db_path)
    first.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    first.close()
    with pytest.raises(SchemaVersionError, match="比程式預期"):
        connect(db_path)


def test_failed_migration_rolls_back_version_and_tables(db_path):
    def broken(c):
        c.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("遷移中途失敗")

    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        migrate(raw)
        with pytest.raises(RuntimeError):
            migrate(raw, migrations=(*migrate_mod.MIGRATIONS, broken))
        assert current_version(raw) == SCHEMA_VERSION
        assert (
            raw.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'half_done'"
            ).fetchone()[0]
            == 0
        )
    finally:
        raw.close()


def test_memory_db_is_refused_not_silently_non_wal():
    with pytest.raises(StorageError, match="WAL"):
        connect(":memory:")


def test_readonly_connect_does_not_create_or_migrate(tmp_path, db_path):
    with pytest.raises(StorageError):
        connect_readonly(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()
    raw = sqlite3.connect(db_path)
    raw.close()  # 空檔，版本 0
    ro = connect_readonly(db_path)
    try:
        assert current_version(ro) == 0
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE x (y)")
    finally:
        ro.close()


def test_import_has_no_side_effects(tmp_path):
    """import storage 不建檔、不讀設定：在空目錄的子程序 import 後目錄仍是空的。"""
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import lore_vault.storage.db, lore_vault.storage.notes, "
            "lore_vault.storage.checks",
        ],
        cwd=tmp_path,
        check=True,
    )
    assert list(tmp_path.iterdir()) == []


def test_v6_adds_episode_prompt_turn_index_to_v5_db(db_path):
    """v5 的庫（含資料）升到 v6：建 episodes(prompt_id, turn_index) 索引，
    A17 的 source_turns 查詢改走索引而非全表掃描。"""
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:5]) == 5
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/m', 'm', 'repo', '2026-09-01T00:00:00.000Z')"
        )
        raw.execute(
            "INSERT INTO episodes (vault, session_id, prompt_id, turn_index, machine, "
            "data, recorded) VALUES ('folder/m', 's', 'p', 0, 'm', '{}', 'x')"
        )
        index_sql = "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?"
        assert raw.execute(index_sql, ("episodes_prompt_turn",)).fetchone() is None

        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:6]) == 6
        assert raw.execute(index_sql, ("episodes_prompt_turn",)).fetchone()
        plan = " ".join(
            str(row[-1])
            for row in raw.execute(
                "EXPLAIN QUERY PLAN SELECT prompt_id, turn_index, vault FROM episodes "
                "WHERE prompt_id IN (?, ?)",
                ("p", "q"),
            )
        )
        assert "episodes_prompt_turn" in plan
        assert raw.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
    finally:
        raw.close()


def test_v7_adds_space_and_existing_vaults_become_dev(db_path):
    """v6 的庫（含 vault 與別名）升到 v7：`vaults.space` 出現，既有 vault 全為 dev
    （A18），別名照舊可在 dev 解析。"""
    from lore_vault.storage.vaults import resolve_write

    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:6]) == 6
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) VALUES "
            "('folder/m', 'm', 'repo', '2026-09-01T00:00:00.000Z'), "
            "('global', 'g', 'global', '2026-09-01T00:00:00.000Z')"
        )
        raw.execute(
            "INSERT INTO vault_aliases (alias, vault) VALUES ('old-m', 'folder/m')"
        )
        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:7]) == 7
        rows = raw.execute("SELECT key, space FROM vaults ORDER BY key").fetchall()
        assert rows == [("folder/m", "dev"), ("global", "dev")]
        assert resolve_write(raw, "old-m", space="dev") == "folder/m"
    finally:
        raw.close()


def test_v12_adds_attribution_and_tombstone_snapshot(db_path):
    """v11 的庫升到 v12（A22）：principal 全回填 xavier；舊 PM 匯入成功的 note
    author／updated_by 為 legacy，其餘（含「id 已存在但非本工具匯入」的清單列）維持
    NULL；舊墓碑沒有快照，取消刪除維持只移除墓碑。"""
    from lore_vault.storage import admin
    from lore_vault.storage.notes import get_note

    ts = "2026-09-01T00:00:00.000Z"
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:11]) == 11
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/m', 'm', 'repo', ?)",
            (ts,),
        )
        for note_id in ("note:imported", "note:conflict", "local-1"):
            raw.execute(
                "INSERT INTO notes (id, vault, title, body, created, updated) "
                "VALUES (?, 'folder/m', ?, '內文', ?, ?)",
                (note_id, note_id, ts, ts),
            )
        for source_id, imported in (
            ("note:imported", ts),
            ("note:conflict", None),
            ("note:gone", ts),
        ):
            raw.execute(
                "INSERT INTO import_sources (source, source_id, note_id, vault, "
                "content_sha256, source_updated, imported_updated, imported_at) "
                "VALUES ('open_notebook', ?, ?, 'folder/m', ?, ?, ?, ?)",
                (source_id, source_id, "0" * 64, ts, imported, imported),
            )
        raw.execute(
            "INSERT INTO note_tombstones (note_id, vault, source, source_id, "
            "deleted_at, reason) VALUES ('note:gone', 'folder/m', 'open_notebook', "
            "'note:gone', ?, 'old')",
            (ts,),
        )
        assert migrate(raw) == SCHEMA_VERSION == 12
        rows = raw.execute(
            "SELECT id, author, principal, updated_by, updated_by_principal "
            "FROM notes ORDER BY id"
        ).fetchall()
        assert rows == [
            ("local-1", None, "xavier", None, "xavier"),
            ("note:conflict", None, "xavier", None, "xavier"),
            ("note:imported", "legacy", "xavier", "legacy", "xavier"),
        ]
        raw.row_factory = sqlite3.Row
        note = get_note(raw, "folder/m", "note:imported", space="dev")
        assert (note.author, note.principal) == ("legacy", "xavier")
        assert admin.find_tombstone(raw, "note:gone")["has_snapshot"] is False
        result = admin.restore_note(raw, "note:gone")
        assert result["restored"] is False and result["note"] is None
    finally:
        raw.close()


def test_snapshot_column_rejects_invalid_json(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO note_tombstones "
            "(note_id, vault, deleted_at, reason, snapshot) "
            "VALUES ('n', 'v', '2026-09-01T00:00:00.000Z', 'r', 'not json')"
        )
