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
