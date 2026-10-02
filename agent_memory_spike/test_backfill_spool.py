"""`hook_stop.py --backfill-spool`：spool 上線前的舊輪次補進 spool。

驗的是三道排除（服務快取已有、spool pending／rejected 已有、schema 不合法）與
dry-run 不寫檔。全部在 tmp_path，不碰 ~/.lore-vault；repo_root 留空，`derive_vault`
不會跑 git。

執行：``python -m pytest agent_memory_spike/test_backfill_spool.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import hook_stop  # noqa: E402
from lore_vault.hooks import spool  # noqa: E402


def _episode(session: str, i: int, **extra) -> dict:
    ep = {
        "prompt_id": f"p{i}", "turn_index": i, "session_id": session,
        "agent": "claude-code", "origin": "human",
        "started_at": "2026-07-25T00:00:00.000Z", "ended_at": "2026-07-25T00:00:01.000Z",
        "cwd": ["/w/Demo"], "repo": "Demo", "repo_root": None, "git_branch": ["main"],
        "cc_version": "2.1.216", "user_text": f"q{i}", "assistant_text": f"a{i}",
        "tool_sequence": [], "tool_calls_total": 0, "mcp_tools": [], "skills": [],
        "files_edited": [], "files_read": [], "symbols_edited": [], "thinking_blocks": 0,
    }
    ep.update(extra)
    return ep


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_stop, "current_machine", lambda: "desk-a")
    ep_dir = tmp_path / "episodes"
    ep_dir.mkdir()
    eps = [_episode("s1", i) for i in range(4)]
    (ep_dir / "s1.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in eps), encoding="utf-8")
    return ep_dir, eps


def _pending(ep_dir: Path) -> set[str]:
    d = hook_stop.spool_dir_for(ep_dir) / spool.PENDING
    return {p.stem for p in d.glob("*.json")} if d.is_dir() else set()


def test_spools_all_local_turns_with_frozen_machine_and_vault(layout):
    ep_dir, eps = layout
    stats = hook_stop.backfill_spool(ep_dir)
    assert stats["written"] == stats["to_spool"] == 4
    assert _pending(ep_dir) == {spool.spool_id(e) for e in eps}
    record = json.loads(
        (hook_stop.spool_dir_for(ep_dir) / spool.PENDING / f"{spool.spool_id(eps[0])}.json")
        .read_text(encoding="utf-8"))
    assert record["episode"]["machine"] == "desk-a"
    assert record["episode"]["vault"].startswith("folder/")
    assert stats["by_vault"] == {record["episode"]["vault"]: 4}


def test_dry_run_writes_nothing(layout):
    ep_dir, _ = layout
    stats = hook_stop.backfill_spool(ep_dir, dry_run=True)
    assert stats["to_spool"] == 4 and stats["written"] == 0
    assert not hook_stop.spool_dir_for(ep_dir).exists()


def test_skips_keys_already_in_service_cache(layout):
    ep_dir, eps = layout
    cache = hook_stop.episode_cache_dir_for(ep_dir)
    cache.mkdir()
    # 服務端那份內容不同（之後被 repair 過）：重送會 conflict，必須不送
    served = dict(eps[1], assistant_text="舊版", vault="folder/demo", machine="desk-a", seq=1)
    (cache / "service.jsonl").write_text(json.dumps(served) + "\n", encoding="utf-8")
    stats = hook_stop.backfill_spool(ep_dir)
    assert stats["in_service"] == 1
    assert spool.spool_id(eps[1]) not in _pending(ep_dir)
    assert len(_pending(ep_dir)) == 3


def test_rejected_turns_are_not_requeued(layout):
    ep_dir, eps = layout
    rejected = hook_stop.spool_dir_for(ep_dir) / spool.REJECTED
    rejected.mkdir(parents=True)
    (rejected / f"{spool.spool_id(eps[2])}.json").write_text(
        json.dumps({"format": 1, "episode": eps[2], "status": "conflict"}), encoding="utf-8")
    stats = hook_stop.backfill_spool(ep_dir)
    assert stats["already_spooled"] == 1
    assert spool.spool_id(eps[2]) not in _pending(ep_dir)


def test_rerun_is_noop(layout):
    ep_dir, _ = layout
    first = hook_stop.backfill_spool(ep_dir)
    pending_dir = hook_stop.spool_dir_for(ep_dir) / spool.PENDING
    mtimes = {p.name: p.stat().st_mtime_ns for p in pending_dir.glob("*.json")}
    second = hook_stop.backfill_spool(ep_dir)
    assert first["written"] == 4
    assert second["already_spooled"] == 4 and second["to_spool"] == second["written"] == 0
    assert {p.name: p.stat().st_mtime_ns for p in pending_dir.glob("*.json")} == mtimes


def test_schema_invalid_turns_are_counted_not_spooled(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_stop, "current_machine", lambda: "desk-a")
    ep_dir = tmp_path / "episodes"
    ep_dir.mkdir()
    bad = _episode("s2", 0)
    del bad["repo_root"]  # backfill_repo_root 推不出時會 pop 掉這欄
    (ep_dir / "s2.jsonl").write_text(
        json.dumps(bad) + "\n" + json.dumps(_episode("s2", 1)) + "\n", encoding="utf-8")
    stats = hook_stop.backfill_spool(ep_dir)
    assert stats["invalid"] == 1 and stats["written"] == 1
    assert spool.spool_id(bad) not in _pending(ep_dir)
    assert any("repo_root" in reason for reason in stats["invalid_by_reason"])
