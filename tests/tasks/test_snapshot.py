"""任務快照（UI-T2）：內容與語料邊界、確定性、大小上限，以及 `sync` 與四個子指令的推送。

推送失敗只在 stderr 警告、exit code 不變；stdout（含 `list --json`）不受影響。
"""

from __future__ import annotations

import base64
import json

import pytest

from lore_vault.tasks import snapshot
from lore_vault.tasks.workspace import load_workspace

from .conftest import (
    SPEC_A,
    VAULT,
    TasksDir,
    delta,
    requirement,
    unreachable_client,
)

# 只出現在 delta 內文／scenario、tasks.md 逐項與 design.md 的字串（不可進快照）
DELTA_SECRET = "機密規格內文zq1"
SCENARIO_SECRET = "機密情境zq2"
TASK_SECRET = "機密子任務zq3"
DESIGN_SECRET = "機密設計zq4"


def _ws(tasks_dir: TasksDir):
    return load_workspace(tasks_dir.root, None, {})


def _by_name(tasks_dir: TasksDir) -> dict:
    return {c["name"]: c for c in snapshot.build_snapshot(_ws(tasks_dir))["changes"]}


def _write_why(tasks_dir: TasksDir, name: str, why: str) -> None:
    path = tasks_dir.change_dir(name) / "proposal.md"
    text = path.read_text(encoding="utf-8").replace("（為什麼要做）", why)
    path.write_text(text, encoding="utf-8")


def _remote(vault) -> bytes:
    hit = vault.blobs[(VAULT, snapshot.SNAPSHOT_KEY)]
    assert hit["mime"] == "application/json"
    return base64.b64decode(hit["content_base64"])


def test_snapshot_fields_and_statuses(tasks_dir: TasksDir):
    tasks_dir.propose("ready", "--skip-specs", "--source", "T-90")
    _write_why(tasks_dir, "ready", "讓 UI 看得到進行中的 change。")
    tasks_dir.propose("blocked", "--skip-specs", "--blocked-by", "D6,D12,D999")
    tasks_dir.propose("dep", "--skip-specs", "--depends-on", "ready,old")
    tasks_dir.propose("auth", "--skip-specs", "--requires-authorization")
    tasks_dir.write_main("demo", SPEC_A)
    tasks_dir.propose(
        "spec",
        deltas={
            "demo": delta(added=[requirement("新功能")], removed=["封存"]),
        },
        complete=False,
    )
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text(
        "schema: spec-driven\nnote_id: note:abc\narchived_at: '2026-10-01'\n",
        encoding="utf-8",
    )
    snap = snapshot.build_snapshot(_ws(tasks_dir))
    assert snap["schema"] == 1 and set(snap) == {"schema", "changes"}
    changes = {c["name"]: c for c in snap["changes"]}
    for entry in changes.values():
        assert set(entry) == snapshot.CHANGE_FIELDS
    ready = changes["ready"]
    assert ready["status"] == "可開工" and ready["reasons"] == []
    assert ready["source"] == "T-90"
    assert ready["why"] == "讓 UI 看得到進行中的 change。"
    assert ready["tasks"] == {"done": 2, "total": 2}
    assert ready["note_id"] is None and ready["archived_at"] is None
    blocked = changes["blocked"]
    assert blocked["status"] == "被擋住"
    assert blocked["blocked_by"] == [
        {"id": "D6", "resolved": False},
        {"id": "D12", "resolved": True},
        {"id": "D999", "resolved": None},
    ]
    assert changes["dep"]["depends_on"] == [
        {"name": "ready", "archived": False},
        {"name": "old", "archived": True},
    ]
    assert changes["auth"]["status"] == "待授權"
    assert changes["auth"]["requires_authorization"] is True
    spec = changes["spec"]
    assert spec["specs"] == [
        {"capability": "demo", "requirement": "新功能", "op": "ADDED"},
        {"capability": "demo", "requirement": "封存", "op": "REMOVED"},
    ]
    assert spec["tasks"] == {"done": 0, "total": 2}
    done = changes["old"]
    assert done["status"] == "已完成"
    assert done["note_id"] == "note:abc" and done["archived_at"] == "2026-10-01"
    # 沒有 proposal.md（或 Why 段為空）為 None
    assert done["why"] is None


def test_unknown_status_when_decisions_missing(tasks_dir: TasksDir):
    tasks_dir.propose("c", "--skip-specs", "--blocked-by", "D12")
    tasks_dir.decisions.unlink()
    entry = _by_name(tasks_dir)["c"]
    assert entry["status"] == "無法判定"
    assert entry["blocked_by"] == [{"id": "D12", "resolved": None}]


def test_snapshot_excludes_corpus_text(tasks_dir: TasksDir):
    """語料邊界：spec delta 全文、scenario、tasks.md 逐項、design.md 都不進快照。"""
    tasks_dir.write_main("demo", "# demo\n\n## Requirements\n")
    tasks_dir.propose(
        "c1",
        deltas={
            "demo": delta(
                added=[
                    requirement(
                        "公開標題", f"系統 SHALL {DELTA_SECRET}。", (SCENARIO_SECRET,)
                    )
                ]
            )
        },
        complete=False,
    )
    tasks = tasks_dir.change_dir("c1") / "tasks.md"
    tasks.write_text(f"# Tasks\n\n- [x] {TASK_SECRET}\n- [ ] 2\n", encoding="utf-8")
    (tasks_dir.change_dir("c1") / "design.md").write_text(
        f"# Design\n\n{DESIGN_SECRET}\n", encoding="utf-8"
    )
    data = snapshot.snapshot_bytes(_ws(tasks_dir)).decode("utf-8")
    for secret in (DELTA_SECRET, SCENARIO_SECRET, TASK_SECRET, DESIGN_SECRET):
        assert secret not in data
    entry = json.loads(data)["changes"][0]
    assert entry["specs"] == [
        {"capability": "demo", "requirement": "公開標題", "op": "ADDED"}
    ]
    assert entry["tasks"] == {"done": 1, "total": 2}


def test_snapshot_bytes_are_deterministic(tasks_dir: TasksDir):
    tasks_dir.propose("a", "--skip-specs", "--blocked-by", "D6")
    tasks_dir.propose("b", "--skip-specs")
    first = snapshot.snapshot_bytes(_ws(tasks_dir))
    assert snapshot.snapshot_bytes(_ws(tasks_dir)) == first
    tasks_dir.set_meta("a", blocked_by=["D12"])
    assert snapshot.snapshot_bytes(_ws(tasks_dir)) != first


def test_size_limit_drops_archived_why_then_refuses(tasks_dir: TasksDir):
    tasks_dir.propose("live", "--skip-specs")
    _write_why(tasks_dir, "live", "進行中" * 100)
    old = tasks_dir.root / "changes" / "archive" / "2026-10-01-old"
    old.mkdir(parents=True)
    (old / ".openspec.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    (old / "proposal.md").write_text("## Why\n\n" + "舊" * 900, encoding="utf-8")
    ws = _ws(tasks_dir)
    full = snapshot.snapshot_bytes(ws)
    data = snapshot.snapshot_bytes(ws, max_bytes=len(full) - 1)
    entries = {c["name"]: c for c in json.loads(data)["changes"]}
    assert entries["old"]["why"] is None and entries["live"]["why"]
    with pytest.raises(snapshot.SnapshotTooLarge):
        snapshot.snapshot_bytes(ws, max_bytes=200)


# ── CLI ──


def test_sync_pushes_snapshot(tasks_dir: TasksDir, vault):
    tasks_dir.propose("c1", "--skip-specs")
    code, out = tasks_dir.run("sync", "--vault", VAULT, client=vault.client)
    assert code == 0 and f"已同步 1 個 change 到 {VAULT}" in out
    assert _remote(vault) == snapshot.snapshot_bytes(_ws(tasks_dir))
    put = [r for r in vault.requests if r["path"] == "/v1/blob_put"][-1]
    assert put["body"]["space"] == "dev" and put["body"]["key"] == "tasks-snapshot"


def test_sync_failure_is_exit_1(tasks_dir: TasksDir):
    code, out = tasks_dir.run("sync", client=unreachable_client)
    assert code == 1 and "sync 失敗" in out


def test_sync_without_client_config_is_exit_1(tasks_dir: TasksDir):
    code, out = tasks_dir.run("sync")
    assert code == 1 and "推送未設定" in out


@pytest.mark.parametrize("command", ["list", "validate"])
def test_read_commands_push_and_never_fail_on_push(tasks_dir: TasksDir, command):
    tasks_dir.propose("c1", "--skip-specs")
    expected = tasks_dir.run(command)
    code, out = tasks_dir.run(command, client=unreachable_client)
    assert (code, out) == expected
    assert "任務快照未同步" in tasks_dir.err


def test_list_json_stays_parseable_when_push_fails(tasks_dir: TasksDir):
    tasks_dir.propose("c1", "--skip-specs")
    code, out = tasks_dir.run("list", "--json", client=unreachable_client)
    assert code == 0 and json.loads(out)[0]["change"] == "c1"
    assert "任務快照未同步" in tasks_dir.err and "任務快照" not in out


def test_each_command_pushes(tasks_dir: TasksDir, vault):
    # propose：建立後推送（vault 由專案目錄 binding 推算）
    code, _ = tasks_dir.run("propose", "c1", "--skip-specs", client=vault.client)
    assert code == 0 and vault.blob_puts == 1
    key = next(iter(vault.blobs))
    assert key[1] == "tasks-snapshot"
    tasks_dir.check_all_tasks("c1")
    for command in (("list",), ("validate", "c1")):
        before = vault.blob_puts
        assert tasks_dir.run(*command, client=vault.client)[0] == 0
        assert vault.blob_puts == before + 1
    before = vault.blob_puts
    code, out = tasks_dir.run("archive", "c1", "--vault", VAULT, client=vault.client)
    assert code == 0, out
    assert vault.blob_puts == before + 1
    entry = json.loads(_remote(vault))["changes"][0]
    assert entry["status"] == "已完成" and entry["note_id"]


def test_failed_propose_and_refused_archive_do_not_push(tasks_dir: TasksDir, vault):
    assert tasks_dir.run("propose", "Bad_Name", client=vault.client)[0] == 1
    tasks_dir.propose("c1", "--skip-specs", "--blocked-by", "D6")
    code, _ = tasks_dir.run("archive", "c1", "--vault", VAULT, client=vault.client)
    assert code == 1
    assert vault.requests == []


def test_propose_push_failure_keeps_exit_0(tasks_dir: TasksDir):
    code, out = tasks_dir.run(
        "propose", "c1", "--skip-specs", client=unreachable_client
    )
    assert code == 0 and "已建立" in out
    assert "任務快照未同步" in tasks_dir.err


def test_archive_pushes_to_the_vault_it_archived_into(tasks_dir: TasksDir, vault):
    """metadata 記的 vault 與專案 binding 不同：推送打到 archive 實際使用的 vault。"""
    recorded = "folder/recorded-elsewhere"
    tasks_dir.propose("c1", "--skip-specs")
    tasks_dir.set_meta("c1", vault=recorded)
    code, out = tasks_dir.run("archive", "c1", client=vault.client)
    assert code == 0, out
    written = {r["body"]["vault"] for r in vault.requests if r["path"] == "/v1/write"}
    assert written == {recorded}
    assert list(vault.blobs) == [(recorded, snapshot.SNAPSHOT_KEY)]
