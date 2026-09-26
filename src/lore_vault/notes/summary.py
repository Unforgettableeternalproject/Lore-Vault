"""查詢結果呈現用的摘要：有 summary 用 summary，缺時以正文首段頂替（A4、A14）。

- `summary_source`：`"summary"`（LLM 摘要）／`"lead"`（正文首段頂替）／
  `"none"`（正文也空）
- 首段 = 第一個去掉標題行、code fence、分隔線後仍有文字的段落（以空行切）；
  空白壓成單一空格，長度上限 `LEAD_MAX_CHARS`——沒有空行的長正文不會整篇當首段回傳
"""

from __future__ import annotations

import re

from lore_vault.schema import Note

SOURCE_SUMMARY = "summary"
SOURCE_LEAD = "lead"
SOURCE_NONE = "none"

# 首段上限：摘要是 1–2 句、約 80 字（D4），首段頂替給到兩倍，仍遠小於全文
LEAD_MAX_CHARS = 160
ELLIPSIS = "…"

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"\s+")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    """截到 `max_chars` 字（含結尾的「…」）。回傳 (結果, 是否截斷)。"""
    if max_chars < 0:
        raise ValueError(f"max_chars 不可為負，得到 {max_chars}")
    if len(text) <= max_chars:
        return text, False
    if max_chars == 0:
        return "", True
    return text[: max_chars - 1] + ELLIPSIS, True


def lead(body: str, max_chars: int = LEAD_MAX_CHARS) -> str | None:
    """正文首段；正文沒有可用文字時回 None。"""
    for paragraph in _PARAGRAPH_SPLIT.split(body):
        lines = []
        for line in paragraph.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped.startswith("```")
                or _RULE.match(stripped)
            ):
                continue
            lines.append(stripped)
        text = _WHITESPACE.sub(" ", " ".join(lines)).strip()
        if text:
            return clip(text, max_chars)[0]
    return None


def display_summary(note: Note) -> tuple[str | None, str]:
    """回傳 (呈現用摘要, summary_source)。"""
    if note.summary is not None:
        return note.summary, SOURCE_SUMMARY
    text = lead(note.body)
    if text is None:
        return None, SOURCE_NONE
    return text, SOURCE_LEAD
