"""純文字與 Markdown 抽取器（T-60）。

- txt（含程式碼等其他純文字檔）：整份一個 segment，locator `offset 0`
  （定長切段在 T-63 之後）。
- md：依 ATX 標題（`#`～`######`）分段，locator 為標題路徑（"設定 > 資料庫"）；
  fenced code block（``` 或 ~~~）內的 `#` 不算標題；第一個標題之前的內容
  locator 為 `offset 0`。不處理 setext 標題（`===`／`---` 底線），`---` 常是
  分隔線或 front matter，誤判代價比漏判高。
"""

from __future__ import annotations

import re

from .base import HEADING_SEPARATOR, Budget, Locator, Segment, decode_text

_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES_RE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def extract_txt(data: bytes, budget: Budget) -> list[Segment]:
    text = budget.add(decode_text(data))
    if not text.strip():
        return []
    return [Segment(text, Locator("offset", 0))]


def _heading(line: str) -> tuple[int, str] | None:
    match = _ATX_RE.match(line)
    if match is None:
        return None
    title = _CLOSING_HASHES_RE.sub("", match.group(2) or "").strip()
    if not title:
        return None
    return len(match.group(1)), title


def extract_md(data: bytes, budget: Budget) -> list[Segment]:
    text = budget.add(decode_text(data))
    segments: list[Segment] = []
    stack: list[tuple[int, str]] = []
    locator = Locator("offset", 0)
    lines: list[str] = []
    fence: str | None = None

    def flush() -> None:
        body = "\n".join(lines).strip("\n")
        if body.strip():
            segments.append(Segment(body, locator))

    for line in text.split("\n"):
        fence_match = _FENCE_RE.match(line)
        if fence is not None:
            # 結束 fence：同字元、長度不短於開頭
            if (
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= len(fence)
                and not line.strip()[len(fence_match.group(1)) :].strip()
            ):
                fence = None
            lines.append(line)
            continue
        if fence_match:
            fence = fence_match.group(1)
            lines.append(line)
            continue
        heading = _heading(line)
        if heading is None:
            lines.append(line)
            continue
        flush()
        level, title = heading
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        locator = Locator("heading", HEADING_SEPARATOR.join(t for _, t in stack))
        lines = [title]
    flush()
    return segments
