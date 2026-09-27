"""T-63／T-64：結構段 → chunk（約 400 token、10–15% 重疊、locator 帶 part／offset）。"""

from __future__ import annotations

import pytest

from lore_vault.documents.chunking import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_segments,
    estimate_tokens,
    join_chunks,
    split_text,
)
from lore_vault.documents.extract import Locator, Segment

ZH = "".join(f"第{i}句說明記憶系統的切段策略與重疊設計。" for i in range(120))
EN = " ".join(f"sentence {i} explains the chunking strategy." for i in range(400))


def _chunks(text: str, locator: Locator, **kw):
    return chunk_segments([Segment(text, locator)], **kw)


def test_defaults_are_400_tokens_with_about_12_percent_overlap():
    assert DEFAULT_MAX_TOKENS == 400
    assert 0.10 <= DEFAULT_OVERLAP_TOKENS / DEFAULT_MAX_TOKENS <= 0.15


def test_short_segment_is_one_chunk_with_original_locator():
    [chunk] = _chunks("短短一頁", Locator("page", 3))
    assert chunk.locator == {"kind": "page", "value": 3}
    assert chunk.overlap == 0 and chunk.idx == 0


@pytest.mark.parametrize("text", [ZH, EN])
def test_long_segment_is_split_within_budget_and_rejoins_exactly(text):
    chunks = _chunks(text, Locator("slide", 2))
    assert len(chunks) > 3
    assert all(estimate_tokens(c.text) <= DEFAULT_MAX_TOKENS for c in chunks)
    assert [c.locator.get("part") for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c.locator["kind"] == "slide" and c.locator["value"] == 2 for c in chunks)
    assert join_chunks((c.text, c.overlap) for c in chunks) == text
    for chunk in chunks[1:]:
        overlap_tokens = estimate_tokens(chunk.text[: chunk.overlap])
        assert 0 < overlap_tokens <= DEFAULT_OVERLAP_TOKENS
    # 中間的 chunk 不會為了找切點而太短
    assert all(estimate_tokens(c.text) >= 0.5 * DEFAULT_MAX_TOKENS for c in chunks[:-1])


def test_chinese_chunk_is_about_266_characters():
    first = _chunks(ZH, Locator("page", 1))[0]
    assert 200 <= len(first.text) <= 270


def test_cut_prefers_sentence_end_and_english_word_boundary():
    for chunk in _chunks(ZH, Locator("page", 1))[:-1]:
        assert chunk.text.endswith("。")
    for chunk in _chunks(EN, Locator("offset", 0))[1:]:
        start = chunk.locator["value"]
        # 不從英文字中間開始：前一個字元是空白
        assert EN[start - 1].isspace() and not EN[start].isspace()


def test_offset_locator_becomes_absolute_start_instead_of_part():
    chunks = _chunks(EN, Locator("offset", 0))
    offsets = [c.locator["value"] for c in chunks]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert all("part" not in c.locator for c in chunks)
    for chunk in chunks:
        start = chunk.locator["value"]
        assert EN[start : start + len(chunk.text)] == chunk.text


def test_heading_locator_keeps_path_and_adds_part():
    chunks = _chunks(ZH, Locator("heading", "設定 > 資料庫"))
    assert chunks[1].locator == {"kind": "heading", "value": "設定 > 資料庫", "part": 2}


def test_tiny_segments_are_kept_and_idx_is_continuous():
    """B1 裁決：短投影片不可被擋；只有全空白的不產生 chunk（抽取器已先濾掉）。"""
    segments = [
        Segment("結論", Locator("slide", 1)),
        Segment("謝謝", Locator("slide", 2)),
        Segment(ZH, Locator("slide", 3)),
    ]
    chunks = chunk_segments(segments)
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert [c.text for c in chunks[:2]] == ["結論", "謝謝"]
    assert join_chunks((c.text, c.overlap) for c in chunks) == "結論\n\n謝謝\n\n" + ZH


def test_hard_cut_when_no_boundary():
    text = "字" * 1000
    pieces = split_text(text)
    assert pieces[0][0] == 0 and len(pieces) > 1
    assert "".join(text[s:e][o:] for s, e, o in pieces) == text


@pytest.mark.parametrize(
    "kw", [{"max_tokens": 0}, {"overlap_tokens": -1}, {"overlap_tokens": 250}]
)
def test_invalid_parameters(kw):
    with pytest.raises(ValueError):
        split_text("x", **kw)
