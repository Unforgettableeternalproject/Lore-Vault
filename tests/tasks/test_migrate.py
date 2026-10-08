"""`python -m lore_vault.tasks migrate`（TASK_LAYER_MCP §5.1、MCP-T7）：
既有 openspec/ 推成服務端版本化內容；可重跑、衝突只回報不覆寫。服務端一律是
`VersionedVault`（假服務），不連真實服務。"""

from __future__ import annotations

import asyncio
import json

import pytest

from lore_vault.tasks import remote_store as rs
from lore_vault.tasks.workspace import load_workspace

from .conftest import SPEC_A, VAULT, TasksDir, delta, requirement
from .versioned_fake import VersionedVault

MOD_ROOT = requirement(
    "資料根目錄", "資料 SHALL 存放於 `~/.x/`。", scenarios=("讀取資料根",)
)
ADD_EXPORT = requirement("匯出", "系統 MUST 能匯出。", scenarios=("匯出",))
NEW_CAP = delta(added=[requirement("新能力", "系統 SHALL 提供新能力。")])


@pytest.fixture
def vv():
    with VersionedVault() as fake:
        yield fake


def _seed(tasks_dir: TasksDir, fake: VersionedVault) -> None:
    """主 spec demo；已封存 old；active c1（改 demo）與 c2（新 capability fresh）。"""
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose("old", deltas={"demo": delta(modified=[MOD_ROOT])})
    code, out = tasks_dir.run("archive", "old", "--vault", VAULT, client=fake.client)
    assert code == 0, out
    tasks_dir.propose("c1", deltas={"demo": delta(added=[ADD_EXPORT])})
    tasks_dir.propose("c2", deltas={"fresh": NEW_CAP})


def _migrate(tasks_dir: TasksDir, fake: VersionedVault, *flags: str):
    return tasks_dir.run("migrate", "--vault", VAULT, *flags, client=fake.client)


def _migrate_json(tasks_dir: TasksDir, fake: VersionedVault, *flags: str):
    code, out = _migrate(tasks_dir, fake, "--json", *flags)
    return code, json.loads(out)


def _local_doc(tasks_dir: TasksDir, name: str) -> dict:
    ws = load_workspace(tasks_dir.root, None, {})
    change = ws.find_active(name)
    assert change is not None
    return rs.local_doc(change)


def test_migrate_pushes_changes_index_and_mirrors(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    writes = vv.writes
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 0, report
    assert report["ok"] is True
    assert report["index_created"] is True
    assert {n: i["action"] for n, i in report["changes"].items()} == {
        "c1": "created",
        "c2": "created",
    }
    assert report["archived"] == {"old": {"action": "indexed", "detail": "archived"}}
    # 服務端 change 內容＝本機工作副本（不含同步欄位），版本 1
    for name in ("c1", "c2"):
        remote = vv.get_json(rs.change_key(name))
        assert vv.version(rs.change_key(name)) == 1
        assert rs.content_digest(remote) == rs.content_digest(
            _local_doc(tasks_dir, name)
        )
        assert rs.REMOTE_VERSION_KEY not in remote["meta"]
        meta = tasks_dir.meta(name)
        assert meta[rs.REMOTE_VERSION_KEY] == 1
        assert meta[rs.REMOTE_DIGEST_KEY] == rs.content_digest(remote)
    # 索引：active 兩個、舊封存只登記名稱，不推內容、不寫 note
    assert vv.get_json(rs.INDEX_KEY)["changes"] == {
        "c1": {"state": "active"},
        "c2": {"state": "active"},
        "old": {"state": "archived"},
    }
    assert vv.get_json(rs.change_key("old")) is None
    assert vv.writes == writes
    # 鏡像：既有主 spec 原樣、新 capability 為 exists:false
    demo = vv.get_json(rs.mirror_key("demo"))
    assert demo["exists"] is True and demo["source"] == "stdio"
    assert demo["text"] == (tasks_dir.root / "specs/demo/spec.md").read_text(
        encoding="utf-8", newline=""
    )
    fresh = vv.get_json(rs.mirror_key("fresh"))
    assert fresh["exists"] is False and fresh["text"] is None


def test_migrated_copy_is_in_sync_for_mcp(tasks_dir: TasksDir, vv):
    """遷移後的本機工作副本，MCP pull／edit 的 local_state 判定為 in_sync。"""
    _seed(tasks_dir, vv)
    assert _migrate(tasks_dir, vv)[0] == 0
    store = rs.RemoteStore(rs.vault_client_post(vv.client()), VAULT)
    ws = load_workspace(tasks_dir.root, None, {})
    for name in ("c1", "c2"):
        remote = asyncio.run(store.get_change(name))
        assert remote is not None
        state = rs.local_state(ws, remote)
        assert (state.state, state.local_version) == ("in_sync", 1)


def test_migrate_rerun_is_noop(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    assert _migrate(tasks_dir, vv)[0] == 0
    puts = vv.blob_puts
    meta = tasks_dir.meta("c1")
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 0
    assert vv.blob_puts == puts
    assert tasks_dir.meta("c1") == meta
    actions = [
        i["action"]
        for group in ("changes", "archived", "mirrors")
        for i in report[group].values()
    ]
    assert set(actions) == {"skipped"}


def test_migrate_conflict_does_not_overwrite(tasks_dir: TasksDir, vv):
    """服務端已有內容不同的同名 change（本機沒有 remote_version）→ 衝突、不動。"""
    _seed(tasks_dir, vv)
    other = rs.new_doc("c1", {"goal": "別台機器建的"}, "# 別的\n", "- [ ] x\n")
    vv.put_json(rs.change_key("c1"), other)
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 1
    assert report["ok"] is False
    assert report["changes"]["c1"]["action"] == "conflict"
    assert report["changes"]["c2"]["action"] == "created"
    assert vv.get_json(rs.change_key("c1")) == other
    assert vv.version(rs.change_key("c1")) == 1
    assert rs.REMOTE_VERSION_KEY not in tasks_dir.meta("c1")


def test_migrate_backfills_when_remote_identical(tasks_dir: TasksDir, vv):
    """上次跑到一半（服務端已建、本機沒回填）：內容相同 → 只回填並補索引。"""
    _seed(tasks_dir, vv)
    vv.put_json(rs.change_key("c1"), _local_doc(tasks_dir, "c1"))
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 0
    assert report["changes"]["c1"] == {
        "action": "backfilled",
        "detail": "服務端內容相同",
        "version": 1,
    }
    assert tasks_dir.meta("c1")[rs.REMOTE_VERSION_KEY] == 1
    assert vv.get_json(rs.INDEX_KEY)["changes"]["c1"] == {"state": "active"}


def test_migrate_reports_synced_but_missing_remote(tasks_dir: TasksDir, vv):
    """本機記錄同步過、服務端卻沒有（被刪？）→ 衝突，不自行重建。"""
    _seed(tasks_dir, vv)
    tasks_dir.set_meta("c1", remote_version=3, remote_digest="x")
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 1
    assert report["changes"]["c1"]["action"] == "conflict"
    assert vv.get_json(rs.change_key("c1")) is None


def test_migrate_archived_conflict(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    vv.put_json(rs.INDEX_KEY, {"schema": 1, "changes": {"old": {"state": "active"}}})
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 1
    assert report["archived"]["old"]["action"] == "conflict"
    assert vv.get_json(rs.INDEX_KEY)["changes"]["old"] == {"state": "active"}


def test_migrate_mirror_conflict_does_not_overwrite(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    mirror = {
        "schema": 1,
        "capability": "demo",
        "exists": True,
        "text": "# 別的內容\n",
        "source": "archive:x",
    }
    vv.put_json(rs.mirror_key("demo"), mirror)
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 1
    assert report["mirrors"]["demo"]["action"] == "conflict"
    assert vv.get_json(rs.mirror_key("demo")) == mirror


def test_migrate_skips_mirror_under_pending_apply(tasks_dir: TasksDir, vv):
    """pending_apply 合併中的 capability：鏡像領先 git，不比也不推。"""
    _seed(tasks_dir, vv)
    doc = rs.new_doc("p1", {"vault": VAULT}, "# P\n", "- [x] t\n")
    doc["state"] = rs.STATE_PENDING_APPLY
    doc["apply"] = {"archived_at": "x", "merged_specs": {"demo": "# 新\n"}}
    vv.put_json(rs.change_key("p1"), doc)
    vv.put_json(
        rs.INDEX_KEY, {"schema": 1, "changes": {"p1": {"state": "pending_apply"}}}
    )
    mirror = {
        "schema": 1,
        "capability": "demo",
        "exists": True,
        "text": "# 新\n",
        "source": "archive:p1",
    }
    vv.put_json(rs.mirror_key("demo"), mirror)
    code, report = _migrate_json(tasks_dir, vv)
    assert code == 0, report
    assert report["mirrors"]["demo"]["action"] == "skipped"
    assert vv.get_json(rs.mirror_key("demo")) == mirror


def test_migrate_dry_run_writes_nothing(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    puts = vv.blob_puts
    meta = tasks_dir.meta("c1")
    code, out = _migrate(tasks_dir, vv, "--dry-run")
    assert code == 0, out
    assert "dry-run" in out
    assert "change c1：將處理（dry-run）" in out
    assert vv.blob_puts == puts
    assert vv.get_json(rs.INDEX_KEY) is None
    assert tasks_dir.meta("c1") == meta


def test_migrate_remote_sync_disabled(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    vv.remote_sync = False
    meta = tasks_dir.meta("c1")
    code, out = _migrate(tasks_dir, vv)
    assert code == 1
    assert "tasks_remote_sync_disabled" in out
    assert tasks_dir.meta("c1") == meta


def test_migrate_requires_configured_client(tasks_dir: TasksDir):
    tasks_dir.write_main("demo", SPEC_A)
    code, out = tasks_dir.run("migrate", "--vault", VAULT)
    assert code == 1
    assert out.startswith("migrate 失敗")


def test_migrate_text_output(tasks_dir: TasksDir, vv):
    _seed(tasks_dir, vv)
    code, out = _migrate(tasks_dir, vv)
    assert code == 0
    assert f"遷移到 {VAULT}" in out
    assert "change c1：已建立 v1" in out
    assert "封存 old：已登記" in out
    assert out.rstrip().splitlines()[-1].startswith("合計：")
