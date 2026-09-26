"""T-61：pdf 抽取（自造樣本）與 CJK 字間空白合併規則。"""

from __future__ import annotations

import pytest

from lore_vault.documents.extract import (
    CORRUPT,
    EMPTY_EXTRACTION,
    ENCRYPTED,
    UNSUPPORTED_FORMAT,
    ExtractionError,
    Limits,
    extract,
    merge_cjk_spacing,
)

LONG_EN = "The quick brown fox jumps over the lazy dog near the riverbank today."


def _code(data: bytes, name: str = "a.pdf", **limits) -> str:
    with pytest.raises(ExtractionError) as info:
        extract(data, name, limits=Limits(**limits) if limits else None)
    return info.value.code


def test_pages_become_segments_with_page_locator(make_pdf):
    data = make_pdf([LONG_EN, "", LONG_EN + " (third page)"])
    result = extract(data, "報告.pdf")
    assert result.format == "pdf"
    assert [s.locator.to_dict() for s in result.segments] == [
        {"kind": "page", "value": 1},
        {"kind": "page", "value": 3},  # 空白頁不產 segment，頁碼不重排
    ]
    assert result.segments[0].text == LONG_EN


def test_blank_pdf_is_empty_extraction(make_pdf):
    assert _code(make_pdf(["", ""])) == EMPTY_EXTRACTION


def test_text_below_threshold_is_empty_extraction(make_pdf):
    """掃描件常只抽出頁碼之類的零星字元：低於門檻不可標成可檢索。"""
    data = make_pdf(["Page 1"])
    assert _code(data) == EMPTY_EXTRACTION
    assert extract(data, "a.pdf", limits=Limits(min_chars=3)).char_count == 5


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-256"])
def test_password_protected_pdf_is_encrypted(make_pdf, algorithm):
    data = make_pdf(
        [LONG_EN],
        encrypt={
            "user_password": "pw",
            "owner_password": "own",
            "algorithm": algorithm,
        },
    )
    assert _code(data) == ENCRYPTED


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-256"])
def test_owner_password_only_pdf_is_readable(make_pdf, algorithm):
    data = make_pdf(
        [LONG_EN],
        encrypt={"user_password": "", "owner_password": "own", "algorithm": algorithm},
    )
    assert extract(data, "a.pdf").segments[0].text == LONG_EN


def test_truncated_pdf_is_corrupt(make_pdf):
    assert _code(make_pdf([LONG_EN])[:200]) == CORRUPT


def test_extension_mismatch_is_unsupported_format(make_docx):
    assert _code(b"just some text, not a pdf") == UNSUPPORTED_FORMAT
    docx_bytes = make_docx(lambda d: d.add_paragraph("x" * 100))
    assert _code(docx_bytes) == UNSUPPORTED_FORMAT


# ── CJK 字間空白合併（T-57 裁決）──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("記 憶 系 統", "記憶系統"),
        ("記\t憶   系統", "記憶系統"),
        ("斷行\n造成的插入", "斷行造成的插入"),
        ("第一段。\n\n第二段", "第一段。\n\n第二段"),  # 段落分隔不動
        ("中文 English 混排", "中文 English 混排"),  # 中英交界空格不動
        ("四個空白    不合併", "四個空白    不合併"),  # 超過 3 個不動
        ("Windows\r\n換行\r\n中文", "Windows\n換行中文"),
        ("句號。 下一句", "句號。下一句"),  # CJK 標點也算
    ],
)
def test_merge_cjk_spacing(raw, expected):
    assert merge_cjk_spacing(raw) == expected
