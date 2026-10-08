"""spec delta 解析與併回試算。"""

from __future__ import annotations

import pytest

from lore_vault.tasks import specs

from .conftest import SPEC_A, delta, requirement


def test_parse_delta_sections_and_fence():
    text = delta(
        added=[requirement("新功能")],
        modified=[requirement("資料根目錄", scenarios=("讀取資料根",))],
        removed=["封存"],
    )
    text += "\n```md\n### Requirement: 圍欄內不算\n```\n"
    plan = specs.parse_delta(text)
    assert [b.name for b in plan.added] == ["新功能"]
    assert [b.name for b in plan.modified] == ["資料根目錄"]
    assert plan.removed == ["封存"]
    assert specs.check_plan(plan) == []


def test_apply_delta_merges_in_order():
    plan = specs.parse_delta(
        delta(
            added=[requirement("新功能")],
            modified=[
                requirement(
                    "資料根目錄",
                    "資料 SHALL 存放於 `~/.x/`。",
                    scenarios=("讀取資料根",),
                )
            ],
            removed=["封存"],
        )
    )
    merged = specs.apply_delta(SPEC_A, plan, "demo", "c1")
    main = specs.parse_main(merged)
    assert [b.name for b in main.blocks] == ["資料根目錄", "新功能"]
    assert "~/.x/" in main.block("資料根目錄").raw
    assert specs.delta_applied(merged, plan) == []
    assert specs.delta_applied(SPEC_A, plan) != []


def test_apply_delta_new_spec_only_added():
    plan = specs.parse_delta(delta(modified=[requirement("X")]))
    with pytest.raises(specs.DeltaError):
        specs.apply_delta(None, plan, "newcap", "c1")
    plan = specs.parse_delta(delta(added=[requirement("X")]))
    merged = specs.apply_delta(None, plan, "newcap", "c1")
    assert merged.startswith("# newcap Specification")
    assert specs.parse_main(merged).block("X") is not None


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (delta(modified=[requirement("資料根目錄", scenarios=("別的",))]), "漏掉"),
        (delta(modified=[requirement("不存在")]), "不在主 spec"),
        (delta(added=[requirement("封存")]), "已存在"),
        (delta(removed=["不存在"]), "不在主 spec"),
        (delta(added=[requirement("無情境", scenarios=())]), "Scenario"),
        (delta(added=[requirement("無規範詞", "系統會運作。")]), "SHALL 或 MUST"),
        (
            "## RENAMED Requirements\n- FROM: `### Requirement: A`\n"
            "- TO: `### Requirement: B`\n",
            "RENAMED",
        ),
        ("## Notes\n\n" + requirement("放錯地方"), "不會被套用"),
        (delta(added=[requirement("A")], removed=["A"]), "同時出現"),
    ],
)
def test_apply_delta_rejects(text, fragment):
    with pytest.raises(specs.DeltaError) as exc:
        specs.apply_delta(SPEC_A, specs.parse_delta(text), "demo", "c1")
    assert any(fragment in m for m in exc.value.messages), exc.value.messages


def test_hash_ignores_line_endings_and_trailing_space():
    raw = "### Requirement: A\n系統 SHALL 運作。\n"
    assert specs.block_hash(raw) == specs.block_hash(raw.replace("\n", "  \r\n"))
    assert specs.block_hash(raw) != specs.block_hash(raw + "多一行\n")


def test_requirement_topic_injective_for_chinese_titles():
    a = specs.requirement_topic(specs.requirement_key("demo", "資料根目錄"))
    b = specs.requirement_topic(specs.requirement_key("demo", "資料封存目錄"))
    assert a != b
    assert a == "req:demo/資料根目錄"
    assert specs.requirement_topic("demo/a b") == "req:demo/a-b"
