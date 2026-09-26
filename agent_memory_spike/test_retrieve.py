"""retrieve 評測語料排除 ambiguous 輪次（注入紀錄帶 ``ambiguous_ids``）的測試。

只排除撞號遷移標記的輪次；其餘（含被注入過的輪次）挑選結果必須與改動前完全相同。

執行：``python -m pytest agent_memory_spike/test_retrieve.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import retrieve  # noqa: E402
from transcript import (ORIGIN_HUMAN, FINGERPRINT_KEY_PREFIX,  # noqa: E402
                        SESSION_WIDE_PROMPT_ID, is_ambiguous, load_ambiguous_turns,
                        prompt_fingerprint)

POOL = [{
    "id": "c-000",
    "scope": "repo-x",
    "statement": "改 a.py 前先看鎖",
    "source_files": ["src/a.py"],
    "source_turns": [["p0", 0]],
}]


def _episode(prompt_id: str, turn_index: int, session_id: str = "s") -> dict:
    return {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "turn_index": turn_index,
        "origin": ORIGIN_HUMAN,
        "user_text": f"請幫我修改 a.py 裡的鎖定邏輯（第 {prompt_id} 輪）",
        "assistant_text": f"好的，{prompt_id} 已修改",
        "files_edited": ["src/a.py"],
        "symbols_edited": [],
        "injected": [],
    }


EPISODES = [_episode("p0", 0), _episode("p1", 1), _episode("p2", 2), _episode("p3", 3)]


def _write_log(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _kept(episodes: list[dict], ambiguous: set) -> list[dict]:
    # 與 retrieve.main 相同的過濾式
    return [e for e in episodes if not is_ambiguous(e, ambiguous)]


def test_load_ambiguous_turns_only_takes_marked_records(tmp_path):
    log = _write_log(tmp_path / "injections.jsonl", [
        {"session_id": "s", "prompt_id": "p1", "injected": ["c-000"]},
        {"session_id": "s", "prompt_id": "p2", "injected": ["c-001"], "ambiguous_ids": ["c-001"]},
        {"session_id": "s", "prompt_id": "p3", "injected": ["c-002"], "ambiguous_ids": []},
        {"session_id": "t", "prompt_fingerprint": "abc", "injected": ["c-001"],
         "ambiguous_ids": ["c-001"]},
    ])
    assert load_ambiguous_turns(log) == {("s", "p2"), ("t", FINGERPRINT_KEY_PREFIX + "abc")}
    assert load_ambiguous_turns(tmp_path / "missing.jsonl") == set()


def test_is_ambiguous_uses_the_same_three_keys_as_build_episode():
    ep = _episode("p1", 1)
    assert not is_ambiguous(ep, set())
    assert is_ambiguous(ep, {("s", "p1")})
    assert is_ambiguous(ep, {("s", SESSION_WIDE_PROMPT_ID)})
    fp = FINGERPRINT_KEY_PREFIX + prompt_fingerprint(ep["user_text"])
    assert is_ambiguous(ep, {("s", fp)})
    assert not is_ambiguous(ep, {("s", "p2"), ("other", "p1")})


def test_unmarked_log_leaves_case_selection_unchanged(tmp_path):
    """沒有 ambiguous_ids（現行資料）：被注入過的輪次照舊入選，結果與改動前逐項相同。"""
    log = _write_log(tmp_path / "injections.jsonl", [
        {"session_id": "s", "prompt_id": "p1", "injected": ["c-000"]},
        {"session_id": "s", "prompt_id": SESSION_WIDE_PROMPT_ID, "injected": ["c-000"]},
    ])
    ambiguous = load_ambiguous_turns(log)
    assert ambiguous == set()
    before = retrieve.build_file_cases(POOL, EPISODES)
    after = retrieve.build_file_cases(POOL, _kept(EPISODES, ambiguous))
    assert after == before
    assert [c["query"] for c in before] == [EPISODES[i]["user_text"] for i in (1, 2, 3)]
    assert (retrieve.build_ground_truth(POOL, _kept(EPISODES, ambiguous))
            == retrieve.build_ground_truth(POOL, EPISODES))


def test_ambiguous_turn_is_excluded_from_case_selection(tmp_path):
    log = _write_log(tmp_path / "injections.jsonl", [
        {"session_id": "s", "prompt_id": "p2", "injected": ["c-001"], "ambiguous_ids": ["c-001"]},
    ])
    before = retrieve.build_file_cases(POOL, EPISODES)
    after = retrieve.build_file_cases(POOL, _kept(EPISODES, load_ambiguous_turns(log)))
    assert after == [c for c in before if "p2" not in c["query"]]
    assert len(after) == len(before) - 1


def _run_dump(tmp_path, monkeypatch, log: Path, name: str) -> list[dict]:
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir(exist_ok=True)
    (episode_dir / "s.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in EPISODES), encoding="utf-8")
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(POOL, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / f"{name}.json"
    monkeypatch.setattr(retrieve, "CONTROL_CONCEPT_PATH", tmp_path / "no-control.json")
    monkeypatch.setattr(retrieve, "load_ambiguous_turns", lambda: load_ambiguous_turns(log))
    monkeypatch.setattr(sys, "argv", [
        "retrieve.py", "--dump-precision", str(out), "--episode-dir", str(episode_dir),
        "--concept-path", str(concept_path),
    ])
    assert retrieve.main() == 0
    return json.loads(out.read_text(encoding="utf-8"))["tasks"]


def test_main_excludes_only_ambiguous_turns_from_precision_tasks(tmp_path, monkeypatch):
    plain = _run_dump(tmp_path, monkeypatch, tmp_path / "missing.jsonl", "plain")
    injected_only = _run_dump(tmp_path, monkeypatch, _write_log(tmp_path / "inj.jsonl", [
        {"session_id": "s", "prompt_id": "p2", "injected": ["c-000"]},
    ]), "injected")
    marked = _run_dump(tmp_path, monkeypatch, _write_log(tmp_path / "amb.jsonl", [
        {"session_id": "s", "prompt_id": "p2", "injected": ["c-001"], "ambiguous_ids": ["c-001"]},
    ]), "marked")
    # 只被注入過（沒有 ambiguous_ids）：與沒有注入紀錄時完全相同
    assert injected_only == plain
    texts = sorted(t["user_text"] for t in plain)
    assert len(texts) == 3
    assert sorted(t["user_text"] for t in marked) == [t for t in texts if "p2" not in t]
