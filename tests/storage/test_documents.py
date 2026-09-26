"""T-58：v8 文件 schema 的約束，與 documents 儲存原語的 vault／space 硬過濾。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lore_vault.documents.extract import ERROR_CODES as EXTRACT_ERROR_CODES
from lore_vault.schema import Vault
from lore_vault.schema.chars import InvalidCharacters
from lore_vault.storage import documents as docs
from lore_vault.storage import migrate as migrate_mod
from lore_vault.storage.errors import NotFound, SpaceRequired, UnknownVault
from lore_vault.storage.migrate import DOCUMENT_ERROR_CODES, SCHEMA_VERSION, migrate
from lore_vault.storage.vaults import upsert_vault

SHA_A = "a" * 64
SHA_B = "b" * 64
TS = "2026-09-01T00:00:00.000Z"


@pytest.fixture
def vaults(conn):
    upsert_vault(conn, Vault(key="folder/dev-a", display="a", kind="repo"))
    upsert_vault(conn, Vault(key="folder/dev-b", display="b", kind="repo"))
    upsert_vault(conn, Vault(key="lore/world", display="w", kind="repo", space="lore"))
    return conn


def _raw_insert(conn, **overrides):
    row = {
        "id": "doc:raw",
        "vault": "folder/dev-a",
        "filename": "a.md",
        "mime": "text/markdown",
        "size_bytes": 1,
        "sha256": SHA_A,
        "status": "pending",
        "error_code": None,
        "created": TS,
        "updated": TS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(
        f"INSERT INTO documents ({cols}) VALUES ({marks})", tuple(row.values())
    )


# ── schema ──────────────────────────────────────────────────────────


def test_error_codes_match_extractor():
    assert tuple(DOCUMENT_ERROR_CODES) == tuple(EXTRACT_ERROR_CODES)
    assert docs.ERROR_CODES == frozenset(EXTRACT_ERROR_CODES)


def test_v7_database_migrates_to_v8(db_path):
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(raw, migrations=migrate_mod.MIGRATIONS[:7]) == 7
        raw.execute(
            "INSERT INTO vaults (key, display, kind, created) VALUES "
            "('folder/m', 'm', 'repo', ?)",
            (TS,),
        )
        assert migrate(raw) == SCHEMA_VERSION == 9
        names = {
            r[0]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "documents",
            "document_chunks",
            "chunk_fts",
            "document_chunk_embeddings",
            "document_tombstones",
            "document_enrichment",
        } <= names
        assert raw.execute("SELECT count(*) FROM vaults").fetchone()[0] == 1
    finally:
        raw.close()


def test_status_check_rejects_unknown_value(vaults):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(vaults, status="done")


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("failed", None),  # 失敗必須帶錯誤碼
        ("ready", "corrupt"),  # 沒失敗不可帶錯誤碼
        ("failed", "weird"),  # 錯誤碼只收白名單
    ],
)
def test_status_and_error_code_must_agree(vaults, status, error_code):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(vaults, status=status, error_code=error_code)


def test_failed_with_known_error_code_is_accepted(vaults):
    for index, code in enumerate(DOCUMENT_ERROR_CODES):
        _raw_insert(vaults, id=f"doc:{index}", status="failed", error_code=code)


@pytest.mark.parametrize("sha", ["A" * 64, "a" * 63, "g" * 64, "../" + "a" * 61])
def test_sha256_check_rejects_malformed(vaults, sha):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(vaults, sha256=sha)


def test_document_requires_existing_vault(vaults):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(vaults, vault="folder/nope")


def test_chunk_locator_must_be_json_and_embeddings_cascade(vaults):
    _raw_insert(vaults)
    with pytest.raises(sqlite3.IntegrityError):
        vaults.execute(
            "INSERT INTO document_chunks (document_id, idx, text, locator) "
            "VALUES ('doc:raw', 0, 't', 'not json')"
        )
    vaults.execute(
        "INSERT INTO document_chunks (seq, document_id, idx, text, locator) "
        "VALUES (7, 'doc:raw', 0, 't', ?)",
        (json.dumps({"kind": "page", "value": 1}),),
    )
    vaults.execute(
        "INSERT INTO document_chunk_embeddings (chunk_seq, dim, vector, updated) "
        "VALUES (7, 2, x'00000000', ?)",
        (TS,),
    )
    vaults.execute("DELETE FROM document_chunks WHERE seq = 7")
    assert (
        vaults.execute("SELECT count(*) FROM document_chunk_embeddings").fetchone()[0]
        == 0
    )


def test_chunk_fts_rows_can_be_deleted(vaults):
    """版本取代與刪除都要能清索引列（因此不用 contentless 表）。"""
    vaults.execute("INSERT INTO chunk_fts (rowid, content) VALUES (1, '記憶 憶系')")
    vaults.execute("DELETE FROM chunk_fts WHERE rowid = 1")
    assert vaults.execute("SELECT count(*) FROM chunk_fts").fetchone()[0] == 0


# ── 儲存原語 ────────────────────────────────────────────────────────


def _insert(conn, vault="folder/dev-a", *, space="dev", sha=SHA_A, **kw):
    return docs.insert_document(
        conn,
        vault,
        space=space,
        filename=kw.pop("filename", "設定.md"),
        mime=kw.pop("mime", "text/markdown"),
        size_bytes=kw.pop("size_bytes", 10),
        sha256=sha,
        **kw,
    )


def test_insert_and_get_roundtrip(vaults):
    doc = _insert(vaults)
    assert doc.id.startswith("doc:")
    assert doc.status == "pending" and doc.version == 1 and doc.error_code is None
    assert doc.vault == "folder/dev-a"
    assert docs.get_document(vaults, "folder/dev-a", doc.id, space="dev") == doc
    assert docs.get_document(vaults, "*", doc.id, space="dev") == doc


def test_space_is_required_and_enforced(vaults):
    doc = _insert(vaults)
    with pytest.raises(SpaceRequired):
        docs.get_document(vaults, "folder/dev-a", doc.id, space=None)  # type: ignore[arg-type]
    # vault 在別的 space：不透露存在性，一律 UnknownVault
    with pytest.raises(UnknownVault):
        docs.get_document(vaults, "folder/dev-a", doc.id, space="lore")
    with pytest.raises(UnknownVault):
        _insert(vaults, "folder/dev-a", space="lore")
    # 同 space 的 "*" 也看不到別 space 的 document
    with pytest.raises(NotFound):
        docs.get_document(vaults, "*", doc.id, space="lore")
    assert docs.list_documents(vaults, "*", space="lore")[0] == []


def test_other_vault_cannot_see_document(vaults):
    doc = _insert(vaults)
    with pytest.raises(NotFound):
        docs.get_document(vaults, "folder/dev-b", doc.id, space="dev")
    assert docs.find_by_sha256(vaults, "folder/dev-b", SHA_A, space="dev") == []


def test_same_blob_in_two_vaults_gets_two_rows(vaults):
    a = _insert(vaults, "folder/dev-a")
    b = _insert(vaults, "folder/dev-b")
    lore = _insert(vaults, "lore/world", space="lore")
    assert len({a.id, b.id, lore.id}) == 3
    assert docs.referenced_sha256(vaults) == {SHA_A}
    assert [d.id for d in docs.find_by_sha256(vaults, "*", SHA_A, space="dev")] in (
        [a.id, b.id],
        [b.id, a.id],
    )


def test_supersedes_bumps_version_and_must_stay_in_vault(vaults):
    old = _insert(vaults)
    new = _insert(vaults, sha=SHA_B, supersedes=old.id)
    assert new.version == 2 and new.supersedes == old.id
    with pytest.raises(NotFound):
        _insert(vaults, "folder/dev-b", sha=SHA_B, supersedes=old.id)
    with pytest.raises(NotFound):
        _insert(vaults, sha=SHA_B, supersedes="doc:missing")


def test_list_documents_pages_and_filters(vaults):
    ids = [_insert(vaults, sha=f"{i:064x}").id for i in range(5)]
    _insert(vaults, "folder/dev-b")
    page, cursor = docs.list_documents(vaults, "folder/dev-a", space="dev", limit=2)
    seen = [d.id for d in page]
    while cursor is not None:
        page, cursor = docs.list_documents(
            vaults, "folder/dev-a", space="dev", limit=2, cursor=cursor
        )
        seen.extend(d.id for d in page)
    assert sorted(seen) == sorted(ids)
    assert docs.list_documents(vaults, "*", space="dev", status="ready")[0] == []
    with pytest.raises(ValueError):
        docs.list_documents(vaults, "*", space="dev", status="bogus")


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha": "A" * 64},
        {"sha": "../../etc/passwd"},
        {"size_bytes": -1},
        {"filename": "  "},
        {"document_id": "nope"},
    ],
)
def test_insert_validates_inputs(vaults, overrides):
    with pytest.raises((ValueError, TypeError)):
        _insert(vaults, **overrides)


def test_insert_rejects_control_characters_in_filename(vaults):
    with pytest.raises(InvalidCharacters):
        _insert(vaults, filename="a\x00.md")
