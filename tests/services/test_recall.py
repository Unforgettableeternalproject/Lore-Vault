"""T-20／T-22（服務端）：recall 的混合排序、降級、A4 契約、預算裁切、vault 洩漏。"""

from __future__ import annotations

import pytest

from lore_vault.recall import UnsupportedKind, recall
from lore_vault.recall.embedder import (
    REASON_ERROR,
    REASON_INVALID,
    REASON_TIMEOUT,
    REASON_UNAVAILABLE,
)
from lore_vault.recall.service import MODE_LEXICAL, MODE_VECTOR
from lore_vault.storage import fts, notes, vectors
from lore_vault.storage.errors import UnknownVault, VaultRequired

from .conftest import DIM, ConstantEmbedder, RaisingEmbedder

A = "folder/a"
B = "folder/b"
ITEM_KEYS = {"id", "kind", "vault", "title", "summary", "summary_source", "score"}
ITEM_KEYS |= {"updated", "author"}


@pytest.fixture
def corpus(conn, add_vault, add_note):
    add_vault(A)
    add_vault(B)
    add_note(
        A,
        "fts",
        "FTS5 中文檢索",
        "trigram 對兩字詞「記憶」靜默回 0 筆，改用 CJK bigram 索引。",
        summary="trigram 搜不到兩字中文詞，改用 bigram。",
    )
    add_note(A, "vec", "向量暴力比對", "NumPy cosine，不建 ANN 索引。")
    add_note(
        A,
        "lock",
        "樂觀鎖",
        "update 帶 expected_updated，版本不符回衝突。",
        embed=False,  # 缺向量：只能由 lexical 命中
    )
    add_note(B, "b-fts", "B 的中文檢索筆記", "記憶 bigram 檢索 另一個專案")
    add_note(B, "b-novec", "B 缺向量", "另一個專案", embed=False)
    return conn


def _ids(result):
    return [item.id for item in result.items]


# ── 混合排序 ────────────────────────────────────────────────────────


def test_mixed_zh_en_query_ranks_the_matching_note_first(corpus, embedder):
    result = recall(
        corpus, "FTS5 中文 bigram 記憶", A, space="dev", embedder=embedder, dim=DIM
    )
    assert not result.degraded
    assert result.legs == ("vector", "lexical")
    assert _ids(result)[0] == "fts"


def test_english_paraphrase_is_rescued_by_vector_leg(corpus, embedder):
    """純英文改寫：lexical 找不到中文 note，向量那一路補上。"""
    query = "chinese full text search"
    lexical = recall(corpus, query, A, space="dev", mode=MODE_LEXICAL)
    assert "fts" not in _ids(lexical)
    hybrid = recall(corpus, query, A, space="dev", embedder=embedder, dim=DIM)
    assert _ids(hybrid)[0] == "fts"


def test_note_without_vector_is_still_fused(corpus, embedder):
    result = recall(
        corpus, "expected_updated 衝突", A, space="dev", embedder=embedder, dim=DIM
    )
    assert "lock" in _ids(result)
    assert result.missing_embeddings == 1
    assert not result.degraded


def test_pure_lexical_and_pure_vector_run_independently(corpus, embedder):
    lexical = recall(corpus, "bigram", A, space="dev", mode=MODE_LEXICAL)
    assert lexical.legs == ("lexical",) and not lexical.degraded
    assert _ids(lexical) == ["fts"]
    assert lexical.missing_embeddings is None
    vector = recall(
        corpus, "向量 cosine", A, space="dev", embedder=embedder, dim=DIM, mode="vector"
    )
    assert vector.legs == ("vector",) and not vector.degraded
    assert _ids(vector)[0] == "vec"
    assert "lock" not in _ids(vector)  # 缺向量，純向量路看不到


# ── 降級：不可偽裝成正常結果 ────────────────────────────────────────


@pytest.mark.parametrize(
    ("make_embedder", "dim", "reason"),
    [
        (lambda: None, DIM, REASON_UNAVAILABLE),
        (lambda: ConstantEmbedder([1.0] * DIM), None, REASON_UNAVAILABLE),
        (lambda: RaisingEmbedder(ConnectionError("拒絕連線")), DIM, REASON_ERROR),
        (lambda: RaisingEmbedder(TimeoutError("timed out")), DIM, REASON_TIMEOUT),
        (lambda: ConstantEmbedder([]), DIM, REASON_INVALID),
        (lambda: ConstantEmbedder(None), DIM, REASON_INVALID),
        (lambda: ConstantEmbedder([1.0] * (DIM - 1)), DIM, REASON_INVALID),
        (lambda: ConstantEmbedder([float("nan")] * DIM), DIM, REASON_INVALID),
        (lambda: ConstantEmbedder([0.0] * DIM), DIM, REASON_INVALID),
    ],
    ids=[
        "none",
        "no-dim",
        "raises",
        "timeout",
        "empty",
        "null",
        "wrong-dim",
        "nan",
        "zero",
    ],
)
@pytest.mark.parametrize("mode", ["hybrid", MODE_VECTOR])
def test_embedder_failure_degrades_to_lexical(corpus, make_embedder, dim, reason, mode):
    result = recall(
        corpus,
        "bigram 記憶",
        A,
        space="dev",
        embedder=make_embedder(),
        dim=dim,
        mode=mode,
    )
    assert result.degraded is True
    assert result.degraded_reason == reason
    assert result.degraded_detail
    assert result.legs == ("lexical",)
    assert _ids(result) == ["fts"]  # 降級後確實跑了 lexical，不是回空
    payload = result.to_dict()
    assert payload["degraded"] is True and payload["degraded_reason"] == reason


def test_healthy_hybrid_is_not_marked_degraded(corpus, embedder):
    result = recall(corpus, "bigram", A, space="dev", embedder=embedder, dim=DIM)
    assert result.degraded is False
    assert result.to_dict()["degraded_reason"] is None


# ── A4 回傳契約與預算 ───────────────────────────────────────────────


def test_items_carry_no_body_and_mark_summary_source(corpus, embedder):
    result = recall(
        corpus, "bigram 衝突 cosine", A, space="dev", embedder=embedder, dim=DIM
    )
    payload = result.to_dict()
    by_id = {item["id"]: item for item in payload["items"]}
    for item in payload["items"]:
        assert set(item) == ITEM_KEYS
        assert item["kind"] == "note"
    assert by_id["fts"]["summary_source"] == "summary"
    assert by_id["lock"]["summary_source"] == "lead"
    assert by_id["lock"]["summary"] == "update 帶 expected_updated，版本不符回衝突。"


def test_lead_is_capped_for_body_without_paragraph_breaks(conn, add_vault, add_note):
    add_vault(A)
    add_note(A, "long", "長文", "# 標題\n" + "很長的正文" * 200, embed=False)
    [item] = recall(conn, "長文", A, space="dev", mode=MODE_LEXICAL).items
    assert item.summary_source == "lead"
    assert len(item.summary) <= 160 and item.summary.endswith("…")
    assert not item.summary.startswith("#")


def test_budget_drops_tail_and_marks_truncation(conn, add_vault, add_note):
    add_vault(A)
    for i in range(5):
        add_note(A, f"n-{i}", f"共同 標題 {i}", summary="摘" * 40, embed=False)
    full = recall(conn, "共同", A, space="dev", mode=MODE_LEXICAL)
    assert len(full.items) == 5 and not full.truncated
    cut = recall(conn, "共同", A, space="dev", mode=MODE_LEXICAL, budget=100)
    assert cut.truncated is True
    assert len(cut.items) == 2 and cut.omitted == 3
    assert cut.used_chars <= 100
    assert [i.id for i in cut.items] == [i.id for i in full.items[:2]]


def test_budget_smaller_than_first_item_clips_it(conn, add_vault, add_note):
    add_vault(A)
    add_note(A, "n-0", "共同", summary="摘" * 100, embed=False)
    add_note(A, "n-1", "共同 共同", summary="摘" * 100, embed=False)
    cut = recall(conn, "共同", A, space="dev", mode=MODE_LEXICAL, budget=30)
    assert cut.truncated is True and cut.omitted == 1
    [item] = cut.items
    assert len(item.title) + len(item.summary) <= 30
    assert item.summary.endswith("…")


# ── kinds 與參數 ────────────────────────────────────────────────────


def test_concept_kind_is_reported_not_ignored(corpus):
    with pytest.raises(UnsupportedKind):
        recall(corpus, "bigram", A, space="dev", kinds=["concept"], mode=MODE_LEXICAL)
    result = recall(
        corpus, "bigram", A, space="dev", kinds=["note", "concept"], mode=MODE_LEXICAL
    )
    assert result.unsupported_kinds == ("concept",)
    assert result.to_dict()["unsupported_kinds"] == ["concept"]
    assert _ids(result) == ["fts"]
    with pytest.raises(ValueError):
        recall(corpus, "bigram", A, space="dev", kinds=["episode"])
    with pytest.raises(TypeError):
        recall(corpus, "bigram", A, space="dev", kinds="note")


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 101}, {"budget": 0}, {"mode": "fuzzy"}],
    ids=["limit0", "limit-big", "budget0", "mode"],
)
def test_invalid_arguments(corpus, kwargs):
    with pytest.raises(ValueError):
        recall(corpus, "bigram", A, space="dev", **kwargs)
    with pytest.raises(ValueError):
        recall(corpus, "   ", A, space="dev")


# ── vault 範圍 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, "", "  "], ids=repr)
def test_missing_vault_raises_before_calling_embedder(corpus, bad):
    spy = RaisingEmbedder(AssertionError("不應呼叫"))
    with pytest.raises(VaultRequired):
        recall(corpus, "bigram", bad, space="dev", embedder=spy, dim=DIM)
    with pytest.raises(UnknownVault):
        recall(corpus, "bigram", "folder/typo", space="dev", embedder=spy, dim=DIM)
    assert spy.calls == 0


def _leaked(conn, embedder) -> list[str]:
    """以 A 的身分走 recall 的各條路徑，回傳看到 B 資料的路徑名稱。"""
    found = []
    query = "記憶 bigram 檢索"
    paths = {
        "hybrid": {"embedder": embedder, "dim": DIM},
        "lexical": {"mode": MODE_LEXICAL},
        "vector": {"embedder": embedder, "dim": DIM, "mode": MODE_VECTOR},
        "degraded": {"embedder": None, "dim": DIM},
    }
    for name, kwargs in paths.items():
        result = recall(conn, query, A, space="dev", **kwargs)
        if any(item.vault != A for item in result.items):
            found.append(name)
        if result.missing_embeddings not in (None, 1):  # A 只有一則缺向量
            found.append(f"{name}:missing_embeddings")
    return found


def test_recall_does_not_leak_across_vaults(corpus, embedder):
    assert _leaked(corpus, embedder) == []


def test_explicit_wildcard_recalls_across_vaults(corpus, embedder):
    result = recall(
        corpus, "記憶 bigram 檢索", "*", space="dev", embedder=embedder, dim=DIM
    )
    assert {item.vault for item in result.items} == {A, B}
    assert result.missing_embeddings == 2


def test_recall_leak_test_is_load_bearing(corpus, embedder, monkeypatch):
    """拿掉 storage 的 vault 過濾（換成恆真），recall 的每條路徑都必須洩漏。"""

    def no_filter(scope, column):
        return "1 = 1", ()

    for module in (notes, fts, vectors):
        monkeypatch.setattr(module, "vault_clause", no_filter)
    assert set(_leaked(corpus, embedder)) == {
        "hybrid",
        "lexical",
        "vector",
        "degraded",
        "hybrid:missing_embeddings",
        "vector:missing_embeddings",
    }
