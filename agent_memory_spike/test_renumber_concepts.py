"""renumber_concepts 的測試：撞號 id 的一次性重新編號與注入紀錄的歧義標記。

執行：``python -m pytest agent_memory_spike/test_renumber_concepts.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from concept_ids import load_high_water  # noqa: E402
from renumber_concepts import main  # noqa: E402
from transcript import load_injections  # noqa: E402

POOL = [
    {"id": "c-000", "statement": "a"},
    {"id": "c-001", "statement": "b"},
    {"id": "c-001", "statement": "b2"},  # 撞號：刪掉 c-001 前後各發一次
    {"id": "c-002", "statement": "c"},
    {"id": "c-001", "statement": "b3"},
    {"id": "c-002", "statement": "c2"},
]

INJECTIONS = [
    {"session_id": "s", "prompt_id": "p1", "injected": ["c-000"]},
    {"session_id": "s", "prompt_id": "p2", "injected": ["c-001", "c-000"]},
    {"session_id": "s", "prompt_id": "p3", "injected": ["c-002", "c-001"]},
]


def _setup(tmp_path, injections=True):
    src = tmp_path / "concepts.json"
    src.write_text(json.dumps(POOL), encoding="utf-8")
    args = ["--in", str(src), "--out", str(tmp_path / "new.json"),
            "--map", str(tmp_path / "map.json")]
    if injections:
        inj = tmp_path / "injections.jsonl"
        # 模擬 Windows 上 hook 以文字模式附加寫出的 CRLF，外加一行壞資料
        body = "".join(json.dumps(r) + "\r\n" for r in INJECTIONS) + "not json\r\n"
        inj.write_bytes(body.encode("utf-8"))
        args += ["--injections", str(inj), "--injections-out", str(tmp_path / "new.jsonl")]
    return src, args


def test_dry_run_writes_nothing(tmp_path):
    src, args = _setup(tmp_path)
    before = src.read_bytes()
    assert main(args) == 0
    assert not (tmp_path / "new.json").exists()
    assert not (tmp_path / "map.json").exists()
    assert not (tmp_path / "new.jsonl").exists()
    assert src.read_bytes() == before


def test_first_occurrence_keeps_id_rest_get_new_numbers(tmp_path):
    src, args = _setup(tmp_path)
    assert main(args + ["--write"]) == 0
    new = json.loads((tmp_path / "new.json").read_text(encoding="utf-8"))
    assert [(c["id"], c["statement"]) for c in new] == [
        ("c-000", "a"), ("c-001", "b"), ("c-003", "b2"),
        ("c-002", "c"), ("c-004", "b3"), ("c-005", "c2"),
    ]
    mapping = json.loads((tmp_path / "map.json").read_text(encoding="utf-8"))
    assert sorted(mapping["duplicated_ids"]) == ["c-001", "c-002"]
    assert [(m["old_id"], m["occurrence"], m["new_id"]) for m in mapping["renumbered"]] == [
        ("c-001", 2, "c-003"), ("c-001", 3, "c-004"), ("c-002", 2, "c-005"),
    ]
    assert mapping["max_id"] == 5
    # 新檔旁的高水位 sidecar：之後蒸餾接著從 c-006 配號
    assert load_high_water(tmp_path / "new.json") == 5
    assert json.loads(src.read_text(encoding="utf-8")) == POOL


def test_new_numbers_start_after_input_high_water(tmp_path):
    src, args = _setup(tmp_path, injections=False)
    (tmp_path / "concepts.id_state.json").write_text('{"max_id": 9}', encoding="utf-8")
    assert main(args + ["--write"]) == 0
    new = json.loads((tmp_path / "new.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in new][2] == "c-010"


def test_injections_with_duplicated_ids_are_marked_not_rewritten(tmp_path):
    _, args = _setup(tmp_path)
    assert main(args + ["--write"]) == 0
    raw = (tmp_path / "new.jsonl").read_bytes().decode("utf-8")
    lines = raw.split("\r\n")
    # 未受影響的行與壞行逐字保留（含 CRLF）
    assert lines[0] == json.dumps(INJECTIONS[0])
    assert lines[3] == "not json"
    marked = [json.loads(line) for line in lines[1:3]]
    assert marked[0]["ambiguous_ids"] == ["c-001"]
    assert marked[1]["ambiguous_ids"] == ["c-002", "c-001"]
    # injected 不改寫、不猜測
    assert [m["injected"] for m in marked] == [r["injected"] for r in INJECTIONS[1:]]


def test_ambiguous_turns_stay_marked_as_injected(tmp_path):
    """歧義紀錄不可被讀取端丟掉：那一輪確實被注入過，丟了就會被當成乾淨語料。"""
    _, args = _setup(tmp_path)
    assert main(args + ["--write"]) == 0
    found = load_injections(tmp_path / "new.jsonl")
    assert found[("s", "p2")] == ["c-001", "c-000"]
    assert found[("s", "p3")] == ["c-002", "c-001"]


@pytest.mark.parametrize("target", ["out", "map", "injections-out"])
def test_refuses_to_overwrite_inputs(tmp_path, target):
    src, args = _setup(tmp_path)
    victim = src if target != "injections-out" else tmp_path / "injections.jsonl"
    idx = args.index(f"--{target}") + 1
    args[idx] = str(victim)
    before = victim.read_bytes()
    with pytest.raises(SystemExit):
        main(args + ["--write"])
    assert victim.read_bytes() == before


def test_injections_flags_must_come_together(tmp_path):
    src, args = _setup(tmp_path, injections=False)
    with pytest.raises(SystemExit):
        main(args + ["--injections", str(tmp_path / "x.jsonl")])
