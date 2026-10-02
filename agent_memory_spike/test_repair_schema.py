"""`hook_stop.py --repair-schema`：把服務端 schema 不合法的舊輪次修成收得下的形狀。

四類對應 2026-10-02 實測的 176 輪：舊欄位 ``files_touched``、缺 ``symbols_edited``、
缺 ``repo_root``、``ended_at`` 早於 ``started_at``。全部在 tmp_path，不碰 ~/.lore-vault。

執行：``python -m pytest agent_memory_spike/test_repair_schema.py -q``
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


def _episode(i: int, **extra) -> dict:
    ep = {
        "prompt_id": f"p{i}", "turn_index": i, "session_id": "s1",
        "agent": "claude-code", "origin": "human",
        "started_at": "2026-07-25T00:00:00.000Z", "ended_at": "2026-07-25T00:00:01.000Z",
        "cwd": ["/w/Demo"], "repo": "Demo", "repo_root": None, "git_branch": ["main"],
        "cc_version": "2.1.216", "user_text": f"q{i}", "assistant_text": f"a{i}",
        "tool_sequence": [], "tool_calls_total": 0, "mcp_tools": [], "skills": [],
        "files_edited": [], "files_read": [], "symbols_edited": [], "thinking_blocks": 0,
        "injected": [],
    }
    ep.update(extra)
    return ep


def _legacy_files_touched(i: int) -> dict:
    ep = _episode(i, repo_root="/w/Demo", files_touched=["/w/Demo/src/a.py", "/w/Demo/src/a.py"])
    for name in ("files_edited", "files_read", "symbols_edited", "injected"):
        ep.pop(name)
    return ep


def _no_symbols(i: int) -> dict:
    ep = _episode(i, files_edited=["src/b.py"])
    ep.pop("symbols_edited")
    ep.pop("injected")
    return ep


def _no_repo_root(i: int) -> dict:
    # 存檔留著以 /w/Demo 為前綴的絕對路徑 → 寫入當時沒有 root，infer_repo_root 推不出
    ep = _episode(i, files_edited=["/w/Demo/c.py"])
    ep.pop("repo_root")
    return ep


def _reversed_span(i: int) -> dict:
    return _episode(i, started_at="2026-07-25T00:00:00.003Z", ended_at="2026-07-25T00:00:00.001Z")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_stop, "current_machine", lambda: "desk-a")
    ep_dir = tmp_path / "episodes"
    ep_dir.mkdir()
    records = [_episode(0), _legacy_files_touched(1), _no_symbols(2),
               _no_repo_root(3), _reversed_span(4)]
    # 合法行刻意用非預設的 JSON 排版：修復不可重新序列化它
    lines = [json.dumps(records[0], separators=(",", ":"))]
    lines += [json.dumps(r) for r in records[1:]]
    path = ep_dir / "s1.jsonl"
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    return ep_dir, path, lines


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_fixture_reproduces_the_four_invalid_kinds(layout):
    _, path, _ = layout
    reasons = [hook_stop.episode_schema_problem(r) for r in _load(path)]
    assert reasons[0] is None
    assert "files_touched" in reasons[1]
    assert "symbols_edited" in reasons[2]
    assert "repo_root" in reasons[3]
    assert "ended_at" in reasons[4]


def test_dry_run_counts_without_writing(layout):
    ep_dir, path, _ = layout
    before = path.read_bytes()
    stats = hook_stop.repair_schema(ep_dir, dry_run=True, backup_dir=ep_dir.parent / "bk")
    assert (stats["invalid"], stats["fixed"], stats["unfixable"]) == (4, 4, 0)
    assert path.read_bytes() == before
    assert not (ep_dir.parent / "bk").exists()


def test_repair_makes_every_line_valid_and_keeps_valid_lines_verbatim(layout):
    ep_dir, path, lines = layout
    original = path.read_bytes()
    backup = ep_dir.parent / "episode_backups" / "t"
    stats = hook_stop.repair_schema(ep_dir, backup_dir=backup)
    assert (stats["fixed"], stats["unfixable"]) == (4, 0)

    assert path.read_text(encoding="utf-8").splitlines()[0] == lines[0]
    assert b"\r" not in path.read_bytes()
    recs = _load(path)
    assert [hook_stop.episode_schema_problem(r) for r in recs] == [None] * 5
    # 備份是改寫前的原檔，而且不在 episode 目錄內（不會被 *.jsonl 撿到）
    assert (backup / "s1.jsonl").read_bytes() == original
    assert sorted(p.name for p in ep_dir.iterdir()) == ["s1.jsonl"]

    legacy, no_sym, no_root, span = recs[1:]
    assert "files_touched" not in legacy
    assert legacy["files_edited"] == ["src/a.py"]  # 依 repo_root 正規化並去重
    assert legacy["files_read"] == [] and legacy["symbols_edited"] == []
    assert no_sym["symbols_edited"] == [] and no_sym["files_edited"] == ["src/b.py"]
    assert "repo_root" in no_root and no_root["repo_root"] is None
    assert (span["started_at"], span["ended_at"]) == (
        "2026-07-25T00:00:00.001Z", "2026-07-25T00:00:00.003Z")
    # injected 的 MISSING 是「不知道」，補 [] 會把舊語料宣告成乾淨語料
    assert "injected" not in legacy and "injected" not in no_sym


def test_repair_is_idempotent(layout):
    ep_dir, path, _ = layout
    hook_stop.repair_schema(ep_dir, backup_dir=ep_dir.parent / "bk1")
    after = path.read_bytes()
    stats = hook_stop.repair_schema(ep_dir, backup_dir=ep_dir.parent / "bk2")
    assert stats["invalid"] == 0
    assert path.read_bytes() == after
    assert not (ep_dir.parent / "bk2").exists()


def test_unfixable_line_is_left_untouched(tmp_path):
    ep_dir = tmp_path / "episodes"
    ep_dir.mkdir()
    bad = json.dumps(_episode(0, origin="robot"))
    path = ep_dir / "s1.jsonl"
    path.write_text(bad + "\n", encoding="utf-8")
    stats = hook_stop.repair_schema(ep_dir, backup_dir=tmp_path / "bk")
    assert (stats["invalid"], stats["fixed"], stats["unfixable"]) == (1, 0, 1)
    assert path.read_text(encoding="utf-8") == bad + "\n"
    assert not (tmp_path / "bk").exists()


def test_backfill_spool_accepts_repaired_turns(layout):
    ep_dir, _, _ = layout
    before = hook_stop.backfill_spool(ep_dir, dry_run=True)
    assert before["invalid"] == 4
    hook_stop.repair_schema(ep_dir, backup_dir=ep_dir.parent / "bk")
    stats = hook_stop.backfill_spool(ep_dir)
    assert stats["invalid"] == 0
    assert stats["written"] == 5
    assert len(list((hook_stop.spool_dir_for(ep_dir) / spool.PENDING).glob("*.json"))) == 5


def test_backfill_repo_root_keeps_unresolvable_root_as_none(tmp_path):
    """--backfill-repo-root 推不出時原本會拔掉欄位，把 --repair-schema 補的 None 拔回非法。"""
    ep_dir = tmp_path / "episodes"
    ep_dir.mkdir()
    rec = _episode(0, files_edited=["/w/Demo/c.py"], repo_root="/w/Demo")
    path = ep_dir / "s1.jsonl"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    hook_stop.backfill_repo_root(ep_dir)
    (out,) = _load(path)
    assert "repo_root" in out and out["repo_root"] is None
    assert hook_stop.episode_schema_problem(out) is None


def test_doctor_warns_on_schema_invalid_turns_until_repaired(layout, monkeypatch, capsys):
    ep_dir, _, _ = layout
    monkeypatch.setattr(hook_stop, "load_injections", lambda: {})
    monkeypatch.setattr(hook_stop, "load_touches", lambda: {})
    monkeypatch.setattr(hook_stop, "find_transcript", lambda _sid: None)

    hook_stop.doctor(ep_dir)
    assert "4 輪不符服務端 Episode schema" in capsys.readouterr().err

    hook_stop.repair_schema(ep_dir, backup_dir=ep_dir.parent / "bk")
    capsys.readouterr()
    hook_stop.doctor(ep_dir)
    assert "不符服務端 Episode schema" not in capsys.readouterr().err
