"""T-20：RRF 融合的邊界（兩路皆空、只有一路、兩路重疊）。"""

from __future__ import annotations

import pytest

from lore_vault.recall.rrf import RRF_K, rrf_fuse


def test_both_legs_empty():
    assert rrf_fuse({"lexical": [], "vector": []}) == []
    assert rrf_fuse({}) == []


@pytest.mark.parametrize("leg", ["lexical", "vector"])
def test_only_one_leg_keeps_its_order(leg):
    other = "vector" if leg == "lexical" else "lexical"
    fused = rrf_fuse({leg: ["a", "b", "c"], other: []})
    assert [f.id for f in fused] == ["a", "b", "c"]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))
    assert fused[2].score == pytest.approx(1 / (RRF_K + 3))
    assert fused[1].ranks == {leg: 2}


def test_overlap_beats_single_leg_top():
    # a：lexical 第 1、vector 第 3；b：lexical 第 2、vector 第 1；d 只在 vector
    fused = rrf_fuse({"lexical": ["a", "b", "c"], "vector": ["b", "d", "a"]})
    by_id = {f.id: f for f in fused}
    assert by_id["b"].score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["a"].score == pytest.approx(1 / 61 + 1 / 63)
    assert [f.id for f in fused] == ["b", "a", "d", "c"]
    assert by_id["a"].ranks == {"lexical": 1, "vector": 3}


def test_doc_missing_from_one_leg_is_not_dropped():
    """缺向量的 note 只出現在 lexical，仍要出現在融合結果。"""
    fused = rrf_fuse({"lexical": ["no-vec", "x"], "vector": ["x", "y"]})
    assert {f.id for f in fused} == {"no-vec", "x", "y"}
    assert [f.id for f in fused][0] == "x"


def test_ties_break_by_best_rank_then_id():
    fused = rrf_fuse({"lexical": ["b", "a"], "vector": ["a", "b"]})
    assert fused[0].score == pytest.approx(fused[1].score)
    assert [f.id for f in fused] == ["a", "b"]


def test_duplicate_ids_within_a_leg_count_once():
    fused = rrf_fuse({"lexical": ["a", "a", "b"]})
    assert [(f.id, f.ranks["lexical"]) for f in fused] == [("a", 1), ("b", 2)]


def test_weights_and_validation():
    fused = rrf_fuse({"lexical": ["a"], "vector": ["b"]}, weights={"vector": 2.0})
    assert [f.id for f in fused] == ["b", "a"]
    with pytest.raises(ValueError):
        rrf_fuse({"lexical": ["a"]}, weights={"vector": 1.0})
    with pytest.raises(TypeError):
        rrf_fuse({"lexical": "abc"})
    with pytest.raises(ValueError):
        rrf_fuse({"lexical": ["a"]}, k=-1)
