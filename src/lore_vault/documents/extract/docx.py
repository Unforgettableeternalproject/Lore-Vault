"""docx 抽取器（T-62）：`python-docx`（本模組內的 `import docx` 是絕對匯入，
指向第三方套件，不是本檔）。

- 內文依段落／表格原順序走訪（`iter_inner_content`），以標題樣式分段：
  樣式名 `Heading N`（Word 內建樣式在 styles.xml 一律存英文名，中文介面亦同），
  另接受 `標題 N`／`标题 N`；locator 為標題路徑。第一個標題之前的內容
  （含沒有任何標題的文件）locator 為 `offset 0`。
- 表格：每列一行、儲存格以 ` | ` 分隔（合併儲存格只取一次）；巢狀表格攤平進儲存格。
- 頁首／頁尾：每節（section）的預設、首頁、偶數頁頁首頁尾，沿用前一節（linked）
  或內容與已收錄者相同的不重複收；locator `header`／`footer`，value 為節序號。
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator
from typing import Any

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from .base import (
    CORRUPT,
    HEADING_SEPARATOR,
    UNSUPPORTED_FORMAT,
    Budget,
    ExtractionError,
    Limits,
    Locator,
    Segment,
    check_ooxml_container,
)

_HEADING_RE = re.compile(r"^(?:heading|標題|标题)\s*([1-9])$", re.IGNORECASE)


def _heading_level(paragraph: Paragraph) -> int | None:
    try:
        style = paragraph.style
    except Exception:  # 樣式定義缺漏的檔案：當一般段落
        return None
    name = (style.name or "") if style is not None else ""
    match = _HEADING_RE.match(name.strip())
    return int(match.group(1)) if match else None


def _block_lines(container: Any) -> Iterator[str]:
    """容器（內文、儲存格、頁首頁尾）內的文字行，段落與表格依原順序。"""
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            yield block.text
        elif isinstance(block, Table):
            yield from _table_lines(block)


def _table_lines(table: Table) -> Iterator[str]:
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            if id(cell._tc) in seen:  # 合併儲存格會重複出現同一個 tc
                continue
            seen.add(id(cell._tc))
            text = " ".join(line.strip() for line in _block_lines(cell) if line.strip())
            cells.append(text)
        if any(cells):
            yield " | ".join(cells)


def _join(lines: Iterable[str]) -> str:
    return "\n".join(lines).strip("\n")


def _body_segments(document: Any, budget: Budget) -> list[Segment]:
    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []
    locator = Locator("offset", 0)
    lines: list[str] = []

    def flush() -> None:
        text = _join(lines)
        if text.strip():
            segments.append(Segment(budget.add(text), locator))

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            level = _heading_level(block)
            title = block.text.strip()
            if level is not None and title:
                flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                locator = Locator(
                    "heading", HEADING_SEPARATOR.join(t for _, t in stack)
                )
                lines = [title]
            else:
                lines.append(block.text)
        elif isinstance(block, Table):
            lines.extend(_table_lines(block))
    flush()
    return segments


def _header_footer_segments(document: Any, budget: Budget) -> list[Segment]:
    segments: list[Segment] = []
    seen: set[str] = set()
    for kind, attrs in (
        ("header", ("header", "first_page_header", "even_page_header")),
        ("footer", ("footer", "first_page_footer", "even_page_footer")),
    ):
        for number, section in enumerate(document.sections, start=1):
            for attr in attrs:
                part = getattr(section, attr)
                if part.is_linked_to_previous:
                    continue
                text = _join(line for line in _block_lines(part))
                if text.strip() and text not in seen:
                    seen.add(text)
                    segments.append(Segment(budget.add(text), Locator(kind, number)))
    return segments


def extract_docx(data: bytes, budget: Budget, limits: Limits) -> list[Segment]:
    check_ooxml_container(data, limits, label="docx")
    try:
        document = docx.Document(io.BytesIO(data))
    except ValueError as exc:
        # zip 合法但主文件內容型別不是 Word（例如 pptx／xlsx 改了副檔名）
        raise ExtractionError(UNSUPPORTED_FORMAT, f"不是 docx：{exc}") from None
    except Exception as exc:
        raise ExtractionError(CORRUPT, f"docx 解析失敗：{exc}") from None
    try:
        return _body_segments(document, budget) + _header_footer_segments(
            document, budget
        )
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(CORRUPT, f"docx 內容解析失敗：{exc}") from None
