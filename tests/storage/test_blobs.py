"""T-59：blob 內容定址儲存（去重、原子寫入、讀取驗雜湊）與 doctor 對帳。"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from lore_vault.doctor import DoctorContext
from lore_vault.doctor.builtin import default_registry
from lore_vault.schema import Vault
from lore_vault.storage import blobs
from lore_vault.storage.documents import insert_document
from lore_vault.storage.vaults import upsert_vault

CONTENT = "世界觀設定：第一章".encode()


@pytest.fixture
def store(tmp_path):
    return blobs.BlobStore(tmp_path / "blobs")


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_put_is_content_addressed_and_deduplicated(store):
    first = store.put(CONTENT)
    second = store.put(CONTENT)
    assert first.sha256 == blobs.sha256_bytes(CONTENT)
    assert first.path == store.root / first.sha256[:2] / first.sha256
    assert first.written and not second.written
    assert _files(store.root) == [first.path]
    assert store.read(first.sha256) == CONTENT


def test_interrupted_write_leaves_no_partial_file(store, monkeypatch):
    def boom(src, dst):
        raise OSError("模擬寫入中斷")

    monkeypatch.setattr(blobs.os, "replace", boom)
    with pytest.raises(OSError, match="模擬寫入中斷"):
        store.put(CONTENT)
    assert _files(store.root) == []


def test_corrupt_blob_is_detected_on_read_and_repaired_on_put(store):
    sha = store.put(CONTENT).sha256
    store.path_for(sha).write_bytes(b"tampered")
    with pytest.raises(blobs.BlobCorrupt):
        store.read(sha)
    assert store.verify(sha) == "mismatch"
    again = store.put(CONTENT)
    assert again.written and again.repaired
    assert store.read(sha) == CONTENT


def test_missing_blob(store):
    sha = blobs.sha256_bytes(b"x")
    assert store.verify(sha) == "missing"
    with pytest.raises(blobs.BlobNotFound):
        store.read(sha)


@pytest.mark.parametrize("bad", ["../" + "a" * 61, "A" * 64, "a" * 10, "a" * 63 + "/"])
def test_malformed_hash_never_becomes_a_path(store, bad):
    with pytest.raises(ValueError):
        store.path_for(bad)


# ── doctor 對帳 ─────────────────────────────────────────────────────


@pytest.fixture
def doc_db(conn, store):
    upsert_vault(conn, Vault(key="folder/a", display="a", kind="repo"))
    result = store.put(CONTENT)
    insert_document(
        conn,
        "folder/a",
        space="dev",
        filename="a.md",
        mime="text/markdown",
        size_bytes=len(CONTENT),
        sha256=result.sha256,
    )
    return conn, result.sha256


def _run(conn, store, **settings):
    ctx = DoctorContext(
        settings={"blob_dir": str(store.root), "environ": {}, **settings},
        resources={"db": conn},
    )
    report = default_registry().run(ctx, categories=["documents"])
    return {r.name: r for r in report.outcomes}


def _status(result):
    return result.result.status.value


def test_doctor_passes_when_blobs_match(doc_db, store):
    conn, _ = doc_db
    results = _run(conn, store)
    assert _status(results["documents.blob_exists"]) == "pass"
    assert _status(results["documents.orphan_blobs"]) == "pass"


def test_doctor_fails_when_referenced_blob_is_deleted(doc_db, store):
    conn, sha = doc_db
    store.path_for(sha).unlink()
    result = _run(conn, store)["documents.blob_exists"]
    assert _status(result) == "fail"
    assert result.result.counts["missing"] == 1


def test_doctor_fails_when_referenced_blob_is_tampered(doc_db, store):
    conn, sha = doc_db
    store.path_for(sha).write_bytes(b"tampered")
    result = _run(conn, store)["documents.blob_exists"]
    assert _status(result) == "fail"
    assert result.result.counts["mismatched"] == 1


def test_doctor_warns_about_orphan_and_unexpected_files(doc_db, store):
    conn, _ = doc_db
    store.put(b"nobody references me")
    (store.root / "stray.bin").write_bytes(b"?")
    result = _run(conn, store)["documents.orphan_blobs"]
    assert _status(result) == "warn"
    assert result.result.counts["orphans"] == 1
    assert result.result.counts["unexpected"] == 1


def test_doctor_warns_about_stale_temp_files_only(doc_db, store):
    conn, sha = doc_db
    tmp = store.path_for(sha).parent / f".{sha}.dead{blobs.TMP_SUFFIX}"
    tmp.write_bytes(b"half")
    old = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    os.utime(tmp, (old, old))
    fresh_now = datetime.fromtimestamp(old + 60, UTC)
    stale_now = datetime.fromtimestamp(old + 2 * blobs.STALE_TMP_SECONDS, UTC)
    assert _status(_run(conn, store, now=fresh_now)["documents.orphan_blobs"]) == "pass"
    result = _run(conn, store, now=stale_now)["documents.orphan_blobs"]
    assert _status(result) == "warn"
    assert result.result.counts["stale_temp"] == 1


def test_doctor_skips_without_blob_dir(doc_db):
    conn, _ = doc_db
    ctx = DoctorContext(settings={"environ": {}}, resources={"db": conn})
    report = default_registry().run(ctx, categories=["documents"])
    blob_checks = {"documents.blob_exists", "documents.orphan_blobs"}
    statuses = {r.result.status.value for r in report.outcomes if r.name in blob_checks}
    assert statuses == {"skipped"}


def test_doctor_reads_blob_dir_from_config(doc_db, store, tmp_path):
    conn, sha = doc_db
    config = tmp_path / "config.toml"
    config.write_text(
        f'[documents]\nblob_dir = "{store.root.as_posix()}"\n', encoding="utf-8"
    )
    store.path_for(sha).unlink()
    ctx = DoctorContext(
        settings={"config": str(config), "environ": {}}, resources={"db": conn}
    )
    report = default_registry().run(ctx, categories=["documents"])
    results = {r.name: r for r in report.outcomes}
    assert _status(results["documents.blob_exists"]) == "fail"
