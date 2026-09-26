"""T-68：`cli.admin delete-document`、`delete-vault --force` 納入文件。

墓碑、chunk／FTS／向量清除、blob 不刪、版本鏈接回、前一版回到索引。
"""

from __future__ import annotations

import io
import json

import pytest

from lore_vault.cli import admin as cli
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.documents.chunking import Chunk
from lore_vault.schema import Vault
from lore_vault.storage import admin
from lore_vault.storage import document_index as index
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.chunk_vectors import set_chunk_embedding
from lore_vault.storage.db import connect
from lore_vault.storage.documents import insert_document
from lore_vault.storage.errors import NotFound
from lore_vault.storage.vaults import upsert_vault

DIM = 4
A = "folder/a"
B = "folder/b"


def _ready(conn, vault, filename, text, sha, supersedes=None):
    doc = insert_document(
        conn,
        vault,
        space="dev",
        filename=filename,
        mime="text/plain",
        size_bytes=len(text),
        sha256=sha,
        supersedes=supersedes,
    )
    assert index.claim(conn, doc.id)
    chunks = [Chunk(0, text, {"kind": "offset", "value": 0})]
    assert index.finish_ready(conn, doc.id, chunks, encoding="utf-8")
    for chunk in index.chunks_of(conn, doc.id):
        if index.chunk_is_indexable(conn, chunk.seq):
            set_chunk_embedding(conn, chunk.seq, [1.0, 0, 0, 0], dim=DIM)
    return doc.id


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "lore.db"
    blobs = BlobStore(tmp_path / "blobs")
    sha_x = blobs.put(b"x").sha256
    sha_y = blobs.put(b"y").sha256
    conn = connect(db)
    try:
        upsert_vault(conn, Vault(key=A, display="a", kind="repo"))
        upsert_vault(conn, Vault(key=B, display="b", kind="repo"))
        v1 = _ready(conn, A, "設定.md", "第一版內容", sha_x)
        v2 = _ready(conn, A, "設定.md", "第二版內容", sha_y, supersedes=v1)
        shared = _ready(conn, B, "共用.md", "同一份 blob", sha_x)
    finally:
        conn.close()
    return db, blobs, {"v1": v1, "v2": v2, "shared": shared, "x": sha_x, "y": sha_y}


def _run(db, *args):
    out = io.StringIO()
    code = cli.main(["--db", str(db), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def _q(db, sql, *params):
    conn = connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _fts(db, doc_id) -> int:
    return _q(
        db,
        "SELECT count(*) FROM chunk_fts WHERE rowid IN "
        "(SELECT seq FROM document_chunks WHERE document_id = ?)",
        doc_id,
    )[0][0]


def _doctor_failures(db, blob_dir) -> list[str]:
    conn = connect(db)
    try:
        report = default_registry().run(
            DoctorContext(
                settings={"embedding_dim": DIM, "blob_dir": str(blob_dir)},
                resources={"db": conn},
            ),
            categories=["documents"],
        )
        return [o.name for o in report.outcomes if o.result.status is Status.FAIL]
    finally:
        conn.close()


def test_delete_document_dry_run_then_delete(world):
    db, blobs, ids = world
    code, plan = _run(
        db, "delete-document", "--space", "dev", "--vault", A, "--id", ids["v2"]
    )
    assert code == 0 and plan["mode"] == "dry_run"
    assert plan["counts"] == {
        "documents": 1,
        "chunks": 1,
        "chunk_fts_rows": 1,
        "chunk_embeddings": 1,
        "document_enrichment": 0,
        "tombstones": 1,
    }
    assert plan["blob_still_referenced"] is False
    assert _q(db, "SELECT count(*) FROM documents")[0][0] == 3

    code, done = _run(
        db,
        "delete-document",
        "--space",
        "dev",
        "--vault",
        A,
        "--id",
        ids["v2"],
        "--yes",
    )
    assert code == 0 and done["mode"] == "deleted"
    assert _q(db, "SELECT count(*) FROM documents WHERE id = ?", ids["v2"])[0][0] == 0
    [[left]] = _q(
        db, "SELECT count(*) FROM document_chunks WHERE document_id = ?", ids["v2"]
    )
    assert left == 0
    grave = _q(db, "SELECT vault, sha256, reason FROM document_tombstones")
    assert [tuple(r) for r in grave] == [(A, ids["y"], admin.DEFAULT_DOCUMENT_REASON)]
    # blob 不刪（變成孤兒，由 doctor 回報）
    assert blobs.exists(ids["y"])
    # 刪掉現行版本：前一版回到索引
    assert _fts(db, ids["v1"]) == 1
    assert _doctor_failures(db, blobs.root) == []


def test_delete_middle_version_relinks_chain(world):
    db, blobs, ids = world
    conn = connect(db)
    try:
        v3 = _ready(conn, A, "設定.md", "第三版內容", ids["y"], supersedes=ids["v2"])
        plan = admin.delete_document(conn, A, ids["v2"], space="dev")
    finally:
        conn.close()
    assert plan.relinked == (v3,)
    assert (
        _q(db, "SELECT supersedes FROM documents WHERE id = ?", v3)[0][0] == ids["v1"]
    )
    # v1 仍被 v3 取代，不回到索引
    assert _fts(db, ids["v1"]) == 0 and _fts(db, v3) == 1
    # 同 blob 仍被 v3 引用
    assert plan.blob_still_referenced is True
    assert _doctor_failures(db, blobs.root) == []


def test_shared_blob_other_vault_still_readable(world):
    db, blobs, ids = world
    conn = connect(db)
    try:
        plan = admin.delete_document(conn, A, ids["v1"], space="dev")
    finally:
        conn.close()
    assert plan.blob_still_referenced is True  # B 的文件也引用 sha_x
    assert blobs.read(ids["x"]) == b"x"
    rows = _q(
        db, "SELECT count(*) FROM document_chunks WHERE document_id = ?", ids["shared"]
    )
    assert rows[0][0] == 1


def test_delete_document_scope_is_enforced(world):
    db, _, ids = world
    conn = connect(db)
    try:
        with pytest.raises(NotFound):
            admin.plan_document_deletion(conn, B, ids["v1"], space="dev")
    finally:
        conn.close()
    code, result = _run(
        db, "delete-document", "--space", "lore", "--vault", A, "--id", ids["v1"]
    )
    assert code == 1 and result is None


def test_delete_vault_includes_documents(world):
    db, blobs, ids = world
    code, plan = _run(db, "delete-vault", "--key", A)
    assert code == 0 and plan["requires_force"] is True
    assert plan["counts"]["documents"] == 2
    assert plan["counts"]["document_tombstones"] == 2
    code, done = _run(db, "delete-vault", "--key", A, "--force", "--yes")
    assert code == 0 and done["mode"] == "deleted"
    assert _q(db, "SELECT vault FROM documents") == _q(
        db, "SELECT vault FROM documents WHERE vault = ?", B
    )
    graves = {r[0] for r in _q(db, "SELECT document_id FROM document_tombstones")}
    assert graves == {ids["v1"], ids["v2"]}
    assert _q(db, "SELECT count(*) FROM chunk_fts")[0][0] == 1
    assert _q(db, "SELECT count(*) FROM document_chunk_embeddings")[0][0] == 1
    assert _doctor_failures(db, blobs.root) == []


def test_empty_documents_vault_needs_force(world):
    db, _, _ = world
    code, plan = _run(db, "delete-vault", "--key", B)
    assert plan["requires_force"] is True  # 只有文件也要 --force
    code, _ = _run(db, "delete-vault", "--key", B, "--yes")
    assert code == 1
