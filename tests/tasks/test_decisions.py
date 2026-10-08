"""DECISIONS.md `### Dn` 解析：規則以現行檔案的 D6／D12／D13 原文片段為 fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lore_vault.tasks.decisions import load_decisions, parse_decisions

from .conftest import DECISIONS_TEXT

REAL = Path(__file__).resolve().parents[2] / "docs" / "hidden" / "DECISIONS.md"


def test_fixture_rules():
    result = parse_decisions(DECISIONS_TEXT)
    assert result == {"D6": False, "D12": True, "D13": True}


def test_section_boundary_does_not_leak():
    # D6 的下一節是「已裁決」，不可因此把 D6 判成解除
    text = "### D6 x\n\n未定\n\n## 已定案\n\n已定案（A1）\n\n### D7 y\n\n已裁決\n"
    assert parse_decisions(text) == {"D6": False, "D7": True}


def test_missing_file_is_unknown(tmp_path):
    assert load_decisions(tmp_path / "nope.md") is None
    assert load_decisions(None) is None


@pytest.mark.skipif(not REAL.is_file(), reason="docs/hidden 不進版控")
def test_real_decisions_file():
    result = load_decisions(REAL)
    assert result is not None
    assert result["D6"] is False
    assert result["D12"] is True
    assert result["D13"] is True


def test_partial_ruling_with_pending_items_is_unresolved():
    text = (
        "### D15 任務層\n\n**待裁決**：\n\n1. a\n2. b\n\n"
        "**已裁決（艾斯維爾 2026-10-08）**：第 1 項\n"
    )
    assert parse_decisions(text) == {"D15": False}


@pytest.mark.skipif(not REAL.is_file(), reason="docs/hidden 不進版控")
def test_real_decisions_d15_partial_is_unresolved():
    assert load_decisions(REAL)["D15"] is False
