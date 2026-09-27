"""孤兒 blob 清理（`plan_gc`／`execute_gc`）：年齡門檻、刪前再確認、佈局限制。"""

from __future__ import annotations

import os
import time

import pytest

from lore_vault.doctor import DoctorContext
from lore_vault.doctor.builtin import default_registry
from lore_vault.schema import Vault
from lore_vault.storage import blobs
from lore_vault.storage.documents import insert_document
from lore_vault.storage.vaults import upsert_vault

HOUR = 3600.0
KEPT = b"referenced content"
ORPHAN = b"nobody references me"


def _age(path, seconds):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def _reference(conn, sha, filename="a.md"):
    return insert_document(
        conn,
        "folder/a",
        space="dev",
        filename=filename,
        mime="text/markdown",
        size_bytes=1,
        sha256=sha,
    )


@pytest.fixture
def store(tmp_path):
    return blobs.BlobStore(tmp_path / "blobs")


@pytest.fixture
def world(conn, store):
    upsert_vault(conn, Vault(key="folder/a", display="a", kind="repo"))
    kept = store.put(KEPT).sha256
    _reference(conn, kept)
    orphan = store.put(ORPHAN).sha256
    for sha in (kept, orphan):
        _age(store.path_for(sha), 2 * HOUR)
    return conn, kept, orphan


def _orphan_status(conn, store):
    ctx = DoctorContext(
        settings={"blob_dir": str(store.root), "environ": {}},
        resources={"db": conn},
    )
    report = default_registry().run(ctx, categories=["documents"])
    return {r.name: r.result.status.value for r in report.outcomes}


def _gc(conn, store, *, min_age=HOUR):
    plan = blobs.plan_gc(conn, store, min_age_seconds=min_age)
    return plan, blobs.execute_gc(conn, store, plan)


def test_plan_is_dry_run_and_lists_prefix_only(world, store):
    conn, kept, orphan = world
    before = sorted(p for p in store.root.rglob("*"))
    plan = blobs.plan_gc(conn, store, min_age_seconds=HOUR)
    assert sorted(p for p in store.root.rglob("*")) == before
    data = plan.to_dict(store.root)
    assert data["orphans"] == [orphan[:12]]
    assert data["counts"]["orphans"] == 1
    assert data["counts"]["orphan_bytes"] == len(ORPHAN)
    assert not conn.in_transaction


def test_execute_deletes_orphan_keeps_referenced_and_doctor_turns_green(world, store):
    conn, kept, orphan = world
    assert _orphan_status(conn, store)["documents.orphan_blobs"] == "warn"
    _, result = _gc(conn, store)
    assert result.deleted == 1 and result.deleted_bytes == len(ORPHAN)
    assert not store.exists(orphan)
    assert store.exists(kept)
    # 孤兒獨佔的子目錄被移除（兩個雜湊前 2 碼不同時）
    if orphan[:2] != kept[:2]:
        assert not (store.root / orphan[:2]).exists()
        assert result.removed_dirs == 1
    statuses = _orphan_status(conn, store)
    assert statuses["documents.orphan_blobs"] == "pass"
    assert statuses["documents.blob_exists"] == "pass"


def test_failed_rows_still_count_as_references(world, store):
    conn, _, orphan = world
    _reference(conn, orphan, filename="b.md")
    conn.execute(
        "UPDATE documents SET status = 'failed', error_code = 'corrupt' "
        "WHERE sha256 = ?",
        (orphan,),
    )
    _, result = _gc(conn, store)
    assert result.deleted == 0
    assert store.exists(orphan)


def test_young_orphans_are_kept(world, store):
    conn, _, orphan = world
    _age(store.path_for(orphan), 10 * 60)
    plan, result = _gc(conn, store)
    assert plan.young_orphans == 1 and not plan.orphans
    assert result.deleted == 0
    assert store.exists(orphan)
    # 門檻調低就會刪
    _, result = _gc(conn, store, min_age=5 * 60)
    assert result.deleted == 1


def test_reference_added_after_plan_is_not_deleted(world, store):
    conn, _, orphan = world
    plan = blobs.plan_gc(conn, store, min_age_seconds=HOUR)
    assert [c.sha256 for c in plan.orphans] == [orphan]
    _reference(conn, orphan, filename="b.md")
    result = blobs.execute_gc(conn, store, plan)
    assert result.deleted == 0 and result.kept_referenced == 1
    assert store.exists(orphan)
    assert _orphan_status(conn, store)["documents.blob_exists"] == "pass"


def test_without_recheck_a_newly_referenced_blob_is_deleted(world, store, monkeypatch):
    """反向：拿掉刪前再確認，規劃後才被引用的 blob 會被誤刪（doctor 變紅）。"""
    conn, _, orphan = world
    plan = blobs.plan_gc(conn, store, min_age_seconds=HOUR)
    _reference(conn, orphan, filename="b.md")
    monkeypatch.setattr(blobs, "sha256_is_referenced", lambda conn, sha: False)
    result = blobs.execute_gc(conn, store, plan)
    assert result.deleted == 1
    assert not store.exists(orphan)
    assert _orphan_status(conn, store)["documents.blob_exists"] == "fail"


def test_dedup_put_after_plan_refreshes_mtime_and_blocks_deletion(world, store):
    """舊孤兒被重新上傳（去重命中、DB 交易還沒提交）：mtime 刷新，gc 不刪。"""
    conn, _, orphan = world
    plan = blobs.plan_gc(conn, store, min_age_seconds=HOUR)
    assert not store.put(ORPHAN).written
    result = blobs.execute_gc(conn, store, plan)
    assert result.deleted == 0 and result.kept_changed == 1
    assert store.exists(orphan)


def test_non_layout_files_are_reported_and_never_touched(world, store):
    conn, kept, _ = world
    stray = store.root / "stray.bin"
    stray.write_bytes(b"?")
    wrong_shard = store.root / "zz" / ("a" * 64)
    wrong_shard.parent.mkdir()
    wrong_shard.write_bytes(b"?")
    odd_name = store.path_for(kept).parent / "notes.txt"
    odd_name.write_bytes(b"?")
    odd_temp = store.path_for(kept).parent / f".{kept}.dead{blobs.TMP_SUFFIX}"
    odd_temp.write_bytes(b"?")
    for path in (stray, wrong_shard, odd_name, odd_temp):
        _age(path, 48 * HOUR)
    plan, result = _gc(conn, store, min_age=0)
    reported = set(plan.to_dict(store.root)["unexpected"])
    assert reported == {"stray.bin", f"zz/{'a' * 64}", f"{kept[:2]}/notes.txt"}
    assert not result.failed
    for path in (stray, wrong_shard, odd_name, odd_temp):
        assert path.exists()
    assert wrong_shard.parent.is_dir()


def test_stale_temp_files_are_deleted_fresh_ones_kept(world, store):
    conn, kept, _ = world
    shard = store.path_for(kept).parent
    stale = shard / f".{kept}.{'0' * 32}{blobs.TMP_SUFFIX}"
    fresh = shard / f".{kept}.{'1' * 32}{blobs.TMP_SUFFIX}"
    stale.write_bytes(b"half")
    fresh.write_bytes(b"half")
    _age(stale, 2 * HOUR)
    _age(fresh, 10 * 60)
    # 門檻 0 也不刪 1 小時內的暫存檔（可能正在寫入）
    plan, result = _gc(conn, store, min_age=0)
    assert [c.path for c in plan.temps] == [stale]
    assert result.temps_deleted == 1 and result.temps_deleted_bytes == 4
    assert not stale.exists() and fresh.exists()
    assert store.exists(kept)


def test_negative_min_age_is_rejected(world, store):
    conn, _, _ = world
    with pytest.raises(ValueError):
        blobs.plan_gc(conn, store, min_age_seconds=-1)
