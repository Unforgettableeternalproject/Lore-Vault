"""蒸餾收料端的測試。

重點是 `scope` 的三態：蒸餾指示要求「跨專案通用則填 null」，所以
**填了 null 是明確表態，缺這個鍵才是沒說**。原本收料端用 `or` 把兩者
合併到 repo 那一邊，780 條原始輸出裡 47 條通用知識被靜默改標成單一 repo，
池子裡 global 的數量因此是精確的零。沒有這組測試，那個 bug 會回來。

執行：``python -m pytest agent_memory_spike/test_distill.py -q``
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from concept_ids import load_high_water, save_high_water, state_path  # noqa: E402
from distill import (  # noqa: E402
    ingest,
    known_repos_from_concepts,
    resolve_scope,
    scope_from_anchors,
)

TASK = {"repo": "Eternity"}

# 「已知合法 vault」要有 >= 2 個不同候選組掛過才算——見
# known_repos_from_concepts。這裡造兩個不同 source_candidate 讓
# JSAI-Functions／JSAI-API 都通過門檻，模擬真實池子裡「這兩個子專案本來就
# 常態出現」的狀態。
KNOWN_REPOS = known_repos_from_concepts(
    [
        {"scope": "JSAI-Functions", "source_candidate": "cand-a"},
        {"scope": "JSAI-Functions", "source_candidate": "cand-b"},
        {"scope": "JSAI-API", "source_candidate": "cand-c"},
        {"scope": "JSAI-API", "source_candidate": "cand-d"},
    ]
)


# --- resolve_scope：三態 ---------------------------------------------------

def test_explicit_null_stays_global():
    """填了 null = 蒸餾者說「這條跨專案通用」，必須保持 None。"""
    assert resolve_scope({"scope": None}, TASK) is None


def test_missing_key_falls_back_to_repo():
    """沒有這個鍵 = 沒說，退回觀察到它的 repo。

    這是與上一項相反的結果，兩者不可合併——這正是原本那個 `or` 的錯。
    """
    assert resolve_scope({}, TASK) == "Eternity"


def test_explicit_string_wins():
    assert resolve_scope({"scope": "AI-Website"}, TASK) == "AI-Website"


def test_blank_string_is_not_a_statement():
    """空字串不是表態，是填壞了——當成沒說，不可當成 global。"""
    assert resolve_scope({"scope": "   "}, TASK) == "Eternity"


def test_global_literals_normalize_to_none():
    """字串形式的 null 也是表態；正規化成 None，否則只有一條注入路徑認得。"""
    for literal in ("null", "None", "global", "*", "GLOBAL"):
        assert resolve_scope({"scope": literal}, TASK) is None, literal


def test_scope_is_trimmed():
    assert resolve_scope({"scope": " Eternity "}, TASK) == "Eternity"


# --- scope_from_anchors／anchors 覆蓋自由文字 scope（2026-10-02 事故）--------
#
# 一組候選同時碰到 monorepo 底下兩個子專案（JSAI-Functions、JSAI-API），
# LLM 把兩條 concept 的 scope 都填成不存在的上位名稱 'JSAI'，兩條都對不上
# 任何 vault，推送整批被服務端拒收。每條自己的 anchors 其實已經指向正確的
# 子專案——resolve_scope 必須拿 anchors 糾正，不能照抄 LLM 的自由文字。


def test_scope_from_anchors_single_head():
    anchors = [
        "JSAI-Functions/src/functions/inspection-fill-reminder.ts",
        "JSAI-Functions/src/functions/inspection-overdue-reminder.ts",
    ]
    assert scope_from_anchors(anchors) == "JSAI-Functions"


def test_scope_from_anchors_ignores_non_path_anchors():
    anchors = ["JSAI-API/src/routes/departments/departments.ts", "container.replace", "patch"]
    assert scope_from_anchors(anchors) == "JSAI-API"


def test_scope_from_anchors_is_none_when_no_path_anchor():
    assert scope_from_anchors(["container.replace", "patch"]) is None
    assert scope_from_anchors([]) is None
    assert scope_from_anchors(None) is None


def test_scope_from_anchors_is_none_when_ambiguous():
    """兩個路徑型錨點開頭不一致——真的跨兩個子專案，不猜。"""
    anchors = ["JSAI-Functions/src/a.ts", "JSAI-API/src/b.ts"]
    assert scope_from_anchors(anchors) is None


def test_resolve_scope_is_corrected_by_disagreeing_anchors():
    """根因回歸測試：拿掉 anchors 覆蓋這段保護，這個斷言會變紅。"""
    task = {"repo": "AI-Website"}
    concept = {
        "scope": "JSAI",
        "anchors": [
            "JSAI-Functions/src/functions/inspection-fill-reminder.ts",
            "JSAI-Functions/src/functions/inspection-overdue-reminder.ts",
        ],
    }
    assert resolve_scope(concept, task, KNOWN_REPOS) == "JSAI-Functions"


def test_resolve_scope_is_corrected_by_disagreeing_anchors_other_subproject():
    task = {"repo": "AI-Website"}
    concept = {
        "scope": "JSAI",
        "anchors": ["JSAI-API/src/routes/departments/departments.ts", "container.replace", "patch"],
    }
    assert resolve_scope(concept, task, KNOWN_REPOS) == "JSAI-API"


def test_resolve_scope_keeps_scope_when_anchors_agree():
    concept = {"scope": "JSAI-API", "anchors": ["JSAI-API/src/x.ts"]}
    assert resolve_scope(concept, TASK, KNOWN_REPOS) == "JSAI-API"


def test_resolve_scope_keeps_scope_when_anchors_ambiguous():
    """anchors 本身歧義時不猜——寧可維持原 scope（即便可能還是錯的，
    但至少不是用猜的去蓋掉一個可能本來就對的值）。"""
    concept = {"scope": "JSAI", "anchors": ["JSAI-Functions/a.ts", "JSAI-API/b.ts"]}
    assert resolve_scope(concept, TASK, KNOWN_REPOS) == "JSAI"


def test_resolve_scope_keeps_scope_when_no_path_anchors():
    concept = {"scope": "Eternity", "anchors": ["container.replace"]}
    assert resolve_scope(concept, TASK, KNOWN_REPOS) == "Eternity"


def test_resolve_scope_does_not_override_without_known_repos():
    """沒有已知名單（或 anchor 指的名字不在名單裡）時絕不猜——這是
    2026-10-02 事故教訓的另一半：單純『anchor 跟 scope 不一樣』不是證據，
    『anchor 指的名字本身是已知合法 vault』才是。"""
    concept = {
        "scope": "JSAI",
        "anchors": ["JSAI-Functions/src/functions/inspection-fill-reminder.ts"],
    }
    assert resolve_scope(concept, TASK) == "JSAI"
    assert resolve_scope(concept, TASK, frozenset()) == "JSAI"


def test_resolve_scope_does_not_override_unestablished_anchor_name():
    """anchor 開頭段落只是個目錄名（例如 'workers'），沒有在已知名單裡
    出現過兩次以上——不當作合法 vault，不覆蓋。"""
    known = known_repos_from_concepts(
        [
            {"scope": "Eternity", "source_candidate": "cand-a"},
            {"scope": "Eternity", "source_candidate": "cand-b"},
        ]
    )
    concept = {"scope": "Eternity", "anchors": ["workers/queue.ts"]}
    assert resolve_scope(concept, TASK, known) == "Eternity"


# --- 端到端：收料寫進 concepts.json ----------------------------------------

def _run_ingest(tmp_path, concepts):
    task = {
        "id": "t-1",
        "repo": "Eternity",
        "source_turns": [["s1", 0]],
        "overlap_files": ["src/a.ts"],
    }
    (tmp_path / "tasks.json").write_text(
        json.dumps({"tasks": [task]}), encoding="utf-8")
    (tmp_path / "out.json").write_text(
        json.dumps([{"id": "t-1", "concepts": concepts}]), encoding="utf-8")

    concept_path = tmp_path / "concepts.json"
    ingest(tmp_path / "out.json", concept_path, tmp_path / "tasks.json",
           tmp_path / "watermark.json")
    return json.loads(concept_path.read_text(encoding="utf-8"))


def test_ingest_preserves_global_and_repo_side_by_side(tmp_path):
    """同一批裡兩種表態要各自保留，不能被收料端抹平成同一個 scope。"""
    result = _run_ingest(tmp_path, [
        {"statement": "通用的那條", "kind": "belief-correction", "scope": None},
        {"statement": "專案特有的那條", "kind": "project-fact"},
    ])
    by_statement = {c["statement"]: c["scope"] for c in result}
    assert by_statement["通用的那條"] is None
    assert by_statement["專案特有的那條"] == "Eternity"


# --- concept id：刪除後新增不可撞號 ---------------------------------------

def _seed_pool(tmp_path, ids):
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(
        [{"id": i, "statement": f"既有 {i}"} for i in ids], ensure_ascii=False),
        encoding="utf-8")
    return concept_path


def _append_ingest(tmp_path, statements, concept_path=None, tag="t-1"):
    task = {"id": tag, "repo": "Eternity", "source_turns": [["s1", 0]],
            "overlap_files": []}
    (tmp_path / f"tasks-{tag}.json").write_text(
        json.dumps({"tasks": [task]}), encoding="utf-8")
    (tmp_path / f"out-{tag}.json").write_text(json.dumps(
        [{"id": tag, "concepts": [{"statement": s} for s in statements]}],
        ensure_ascii=False), encoding="utf-8")
    concept_path = concept_path or tmp_path / "concepts.json"
    ingest(tmp_path / f"out-{tag}.json", concept_path, tmp_path / f"tasks-{tag}.json",
           tmp_path / "watermark.json", append=True)
    return json.loads(concept_path.read_text(encoding="utf-8"))


def test_append_after_deletion_does_not_reuse_live_id(tmp_path):
    """收斂刪掉 c-001 後池子剩兩條，舊規則 ``c-{len:03d}`` 會發出仍存在的 c-002。"""
    _seed_pool(tmp_path, ["c-000", "c-002"])
    result = _append_ingest(tmp_path, ["新的一條"])
    ids = [c["id"] for c in result]
    assert len(ids) == len(set(ids)), ids
    assert ids[-1] == "c-003"


def test_deleted_tail_id_is_never_reissued(tmp_path):
    """刪掉的是最大號（c-002）時，現存最大號退回 c-001；高水位要擋住 c-002 被重發。"""
    concept_path = _seed_pool(tmp_path, ["c-000", "c-001", "c-002"])
    _append_ingest(tmp_path, ["第一批"], tag="t-1")  # c-003，高水位 3
    pool = json.loads(concept_path.read_text(encoding="utf-8"))
    concept_path.write_text(json.dumps([c for c in pool if c["id"] in ("c-000", "c-001")]),
                            encoding="utf-8")
    result = _append_ingest(tmp_path, ["第二批"], tag="t-2")
    assert result[-1]["id"] == "c-004"
    assert load_high_water(concept_path) == 4


def test_file_max_wins_over_stale_or_missing_sidecar(tmp_path):
    """sidecar 遺失或比檔案小（手動編輯、舊資料）時，以檔內現存最大號兜底。"""
    concept_path = _seed_pool(tmp_path, ["c-000", "c-009"])
    assert not state_path(concept_path).exists()
    assert _append_ingest(tmp_path, ["a"], tag="t-1")[-1]["id"] == "c-010"
    save_high_water(concept_path, 3)  # 不可往下寫
    assert load_high_water(concept_path) == 10
    state_path(concept_path).write_text('{"max_id": 2}', encoding="utf-8")
    assert _append_ingest(tmp_path, ["b"], tag="t-2")[-1]["id"] == "c-011"


def test_sidecar_wins_over_manually_trimmed_file(tmp_path):
    concept_path = _seed_pool(tmp_path, ["c-000"])
    save_high_water(concept_path, 5)
    assert _append_ingest(tmp_path, ["a"])[-1]["id"] == "c-006"


def test_full_ingest_also_continues_from_high_water(tmp_path):
    """非增量的全量收回會覆寫池子，但舊 id 可能還在注入紀錄與服務端，不可從 c-000 重來。"""
    _seed_pool(tmp_path, ["c-000", "c-001"])
    result = _run_ingest(tmp_path, [{"statement": "全量的一條"}])
    assert [c["id"] for c in result] == ["c-002"]


def test_repeated_ingest_keeps_ids_unique(tmp_path):
    _seed_pool(tmp_path, [])
    for n in range(3):
        result = _append_ingest(tmp_path, [f"第 {n} 批 a", f"第 {n} 批 b"], tag=f"t-{n}")
    ids = [c["id"] for c in result]
    assert ids == [f"c-{i:03d}" for i in range(6)]


def test_concurrent_ingest_does_not_collide(tmp_path):
    """兩個收料同時跑：沒有鎖會各自讀到同一個最大號並發出同一個 id（或互相蓋掉池子）。"""
    concept_path = _seed_pool(tmp_path, ["c-000"])
    workers = 4
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def run(n):
        try:
            barrier.wait()
            _append_ingest(tmp_path, [f"並行 {n}"], tag=f"t-{n}")
        except BaseException as exc:  # noqa: BLE001 — 收集後在主執行緒斷言
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    ids = [c["id"] for c in json.loads(concept_path.read_text(encoding="utf-8"))]
    assert sorted(ids) == [f"c-{i:03d}" for i in range(workers + 1)]
    assert load_high_water(concept_path) == workers
