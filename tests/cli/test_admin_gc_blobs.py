"""`cli.admin gc-blobs`：預設 dry-run、--yes 才刪、年齡門檻、刪後 doctor 綠。"""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from lore_vault.cli import admin as cli
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.schema import Vault
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect
from lore_vault.storage.documents import insert_document
from lore_vault.storage.vaults import upsert_vault

HOUR = 3600.0


def _age(path, seconds):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def _reference(db, sha, filename):
    conn = connect(db)
    try:
        insert_document(
            conn,
            "folder/a",
            space="dev",
            filename=filename,
            mime="text/plain",
            size_bytes=1,
            sha256=sha,
        )
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "lore.db"
    store = BlobStore(tmp_path / "blobs")
    kept = store.put(b"kept").sha256
    orphan = store.put(b"orphan").sha256
    conn = connect(db)
    try:
        upsert_vault(conn, Vault(key="folder/a", display="a", kind="repo"))
    finally:
        conn.close()
    _reference(db, kept, "a.md")
    for sha in (kept, orphan):
        _age(store.path_for(sha), 2 * HOUR)
    return db, store, kept, orphan


def _run(db, store, *args):
    out = io.StringIO()
    code = cli.main(
        ["--db", str(db), "gc-blobs", "--blob-dir", str(store.root), *args],
        stdout=out,
    )
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def _files(store):
    return sorted(p for p in store.root.rglob("*"))


def _orphan_status(db, store):
    conn = connect(db)
    try:
        ctx = DoctorContext(
            settings={"blob_dir": str(store.root), "environ": {}},
            resources={"db": conn},
        )
        report = default_registry().run(ctx, categories=["documents"])
    finally:
        conn.close()
    return {r.name: r.result.status.value for r in report.outcomes}


def test_dry_run_lists_prefixes_and_touches_nothing(world):
    db, store, _, orphan = world
    before = _files(store)
    code, out = _run(db, store)
    assert code == 0
    assert out["mode"] == "dry_run"
    assert out["orphans"] == [orphan[:12]]
    assert out["counts"]["orphans"] == 1
    assert out["counts"]["orphan_bytes"] == len(b"orphan")
    assert "hint" in out
    assert _files(store) == before


def test_yes_deletes_orphan_keeps_referenced_and_doctor_is_green(world):
    db, store, kept, orphan = world
    assert _orphan_status(db, store)["documents.orphan_blobs"] == "warn"
    code, out = _run(db, store, "--yes")
    assert code == 0
    assert out["mode"] == "deleted"
    assert out["deleted"]["orphans"] == 1
    assert out["deleted"]["orphan_bytes"] == len(b"orphan")
    assert not store.exists(orphan)
    assert store.exists(kept)
    statuses = _orphan_status(db, store)
    assert statuses["documents.orphan_blobs"] == "pass"
    assert statuses["documents.blob_exists"] == "pass"


def test_min_age_threshold(world):
    db, store, _, orphan = world
    _age(store.path_for(orphan), 30 * 60)
    code, out = _run(db, store, "--yes")
    assert code == 0
    assert out["counts"]["young_orphans_kept"] == 1
    assert out["deleted"]["orphans"] == 0
    assert store.exists(orphan)
    code, out = _run(db, store, "--min-age-hours", "0.25", "--yes")
    assert out["deleted"]["orphans"] == 1
    assert not store.exists(orphan)


def test_reference_added_after_dry_run_is_not_deleted(world):
    db, store, _, orphan = world
    code, out = _run(db, store)
    assert out["orphans"] == [orphan[:12]]
    _reference(db, orphan, "b.md")
    code, out = _run(db, store, "--yes")
    assert code == 0
    assert out["deleted"]["orphans"] == 0
    assert store.exists(orphan)


def test_non_layout_files_are_reported_not_deleted(world):
    db, store, _, _ = world
    stray = store.root / "stray.bin"
    stray.write_bytes(b"?")
    _age(stray, 48 * HOUR)
    code, out = _run(db, store, "--yes")
    assert code == 0
    assert out["unexpected"] == ["stray.bin"]
    assert stray.exists()


def test_negative_min_age_is_an_argument_error(world):
    db, store, _, _ = world
    with pytest.raises(SystemExit) as exc:
        _run(db, store, "--min-age-hours", "-1")
    assert exc.value.code == 2


def test_missing_blob_dir_fails(world, tmp_path):
    db, _, _, _ = world
    code, out = _run(db, BlobStore(tmp_path / "nope"))
    assert code == 1 and out is None
