"""DECISIONS.md 的 `### Dn` 小節解析：判定 `blocked_by` 是否解除。

規則（對現行檔案測過：D6 未解除、D12／D13 已解除）：
- 小節範圍：`### Dn ...` 起，到下一個 `### ` 或 `## ` 標題為止
- 小節內出現「已裁決」「已定案」或「YYYY-MM-DD 裁決」
  （如「艾斯維爾 2026-09-27 裁決：」）即視為已解除；
  其餘（如 D6「不在本次範圍」）一律未解除
- 檔案不存在 → `None`，呼叫端標「無法判定」，不當成解除
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADER = re.compile(r"^###\s+(D\d+)\b")
_BOUNDARY = re.compile(r"^#{2,3}\s")
_RESOLVED = re.compile(r"已裁決|已定案|\d{4}-\d{2}-\d{2}\s*裁決")
DECISION_ID = re.compile(r"^D\d+$")


def parse_decisions(text: str) -> dict[str, bool]:
    """`{"D6": False, "D12": True, ...}`。"""
    result: dict[str, bool] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            result[current] = bool(_RESOLVED.search("\n".join(buf)))

    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if not in_fence and _BOUNDARY.match(line):
            flush()
            m = _HEADER.match(line)
            current = m.group(1) if m else None
            buf = []
            continue
        if current is not None:
            buf.append(line)
    flush()
    return result


def load_decisions(path: Path | None) -> dict[str, bool] | None:
    if path is None:
        return None
    try:
        return parse_decisions(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
