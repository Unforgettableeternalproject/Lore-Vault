"""`lore_vault.schema.chars`：禁用字元的定義、拒收與清理。

hook 也 import 這個模組，必須是純標準庫。
"""

from __future__ import annotations

import pytest

from lore_vault.schema.chars import (
    FORBIDDEN_CONTROL,
    InvalidCharacters,
    check_fields,
    find_invalid,
    sanitize_text,
    sanitize_value,
)


def test_forbidden_set_is_c0_minus_tab_lf_cr():
    assert len(FORBIDDEN_CONTROL) == 29
    assert not {"\t", "\n", "\r"} & FORBIDDEN_CONTROL
    assert "\x00" in FORBIDDEN_CONTROL and "\x1f" in FORBIDDEN_CONTROL
    assert "\x7f" not in FORBIDDEN_CONTROL and " " not in FORBIDDEN_CONTROL


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("正常\t文字\r\n", None),
        ("a\x00b", (1, "control")),
        ("中文\x1b", (2, "control")),
        ("x\ud83d", (1, "surrogate")),
        ("😀", None),  # 成對的 surrogate 在 Python str 裡是單一碼位，不算
    ],
)
def test_find_invalid(text, expected):
    assert find_invalid(text) == expected


def test_check_fields_reports_field_and_first_index_without_content():
    with pytest.raises(InvalidCharacters) as info:
        check_fields({"title": "ok", "topics": ["a", "秘密\x00\x01"]})
    exc = info.value
    assert (exc.field, exc.index, exc.codepoint, exc.kind) == (
        "topics[1]",
        2,
        0,
        "control",
    )
    assert "秘密" not in str(exc) and "U+0000" in str(exc)
    check_fields({"title": "ok", "supersedes": None, "links": ()})


def test_sanitize_text_replacements_and_count():
    text, count = sanitize_text("a\x00b\x01c\x1fd\ud800e\tf")
    assert text == "a\\0b\\x01c\\x1fd\\ud800e\tf"
    assert count == 4
    assert sanitize_text("乾淨") == ("乾淨", 0)


def test_sanitize_is_idempotent():
    once, _ = sanitize_text("x\x00y\x02")
    assert sanitize_text(once) == (once, 0)


def test_sanitize_value_walks_nested_json_without_mutating():
    original = {"a": ["x\x00", {"k\x01": "ok"}], "n": 3, "t": ("y\x02",)}
    cleaned, count = sanitize_value(original)
    assert count == 3
    assert cleaned == {"a": ["x\\0", {"k\\x01": "ok"}], "n": 3, "t": ("y\\x02",)}
    assert original["a"][0] == "x\x00"
    same = {"a": ["ok"]}
    assert sanitize_value(same) == (same, 0) and sanitize_value(same)[0] is same
