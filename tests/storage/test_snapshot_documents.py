"""T-69：快照白名單明確排除文件（不進降級）。"""

from __future__ import annotations

import sqlite3

import pytest

from lore_vault.documents.chunking import Chunk
from lore_vault.schema import Vault
from lore_vault.storage import document_index as index
from lore_vault.storage import snapshot
from lore_vault.storage.documents import insert_document
from lore_vault.storage.vaults import upsert_vault

DOCUMENT_TABLES = {
    "documents",
    "document_chunks",
    "chunk_fts",
    "document_chunk_embeddings",
    "document_tombstones",
    "document_enrichment",
}


def test_whitelist_excludes_every_document_table(conn):
    assert not set(snapshot.SNAPSHOT_TABLES) & DOCUMENT_TABLES
    assert DOCUMENT_TABLES <= set(snapshot.SNAPSHOT_EXCLUDED_TABLES)
    # 日後新增的文件相關表（document*／chunk*）也必須明確列入排除清單
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND (name LIKE 'document%' OR name LIKE 'chunk%')"
        )
    }
    base = {n for n in names if not n.startswith("chunk_fts_")}
    assert base <= set(snapshot.SNAPSHOT_EXCLUDED_TABLES), base


@pytest.fixture
def live_with_documents(conn, db_path):
    upsert_vault(conn, Vault(key="folder/a", display="a", kind="repo"))
    doc = insert_document(
        conn,
        "folder/a",
        space="dev",
        filename="a.md",
        mime="text/markdown",
        size_bytes=1,
        sha256="a" * 64,
    )
    assert index.claim(conn, doc.id)
    index.finish_ready(
        conn,
        doc.id,
        [Chunk(0, "文件內容", {"kind": "offset", "value": 0})],
        encoding="utf-8",
    )
    assert conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0] == 1
    return db_path


def test_snapshot_contains_no_document_rows(live_with_documents, tmp_path):
    dest = tmp_path / "snap.db"
    snapshot.build_snapshot(live_with_documents, dest)
    snap = sqlite3.connect(dest)
    try:
        for table in DOCUMENT_TABLES:
            assert snap.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert snap.execute("SELECT count(*) FROM vaults").fetchone()[0] == 1
    finally:
        snap.close()


def test_guard_refuses_snapshot_with_document_rows(
    live_with_documents, tmp_path, monkeypatch
):
    """拿掉白名單（模擬把文件也複製進快照）時，產生快照必須失敗且不留檔。"""
    dest = tmp_path / "snap.db"
    real_migrate = snapshot.migrate

    def leaky_migrate(conn):
        version = real_migrate(conn)
        conn.execute(
            "INSERT INTO vaults (key, display, kind, created) "
            "VALUES ('folder/x', 'x', 'repo', '2026-01-01T00:00:00.000Z')"
        )
        conn.execute(
            "INSERT INTO documents (id, vault, filename, mime, size_bytes, sha256, "
            "status, created, updated) VALUES ('doc:x', 'folder/x', 'x.md', 'm', 1, "
            "?, 'pending', 't', 't')",
            ("b" * 64,),
        )
        return version

    monkeypatch.setattr(snapshot, "migrate", leaky_migrate)
    with pytest.raises(snapshot.SnapshotError, match="documents"):
        snapshot.build_snapshot(live_with_documents, dest)
    assert not dest.exists()
