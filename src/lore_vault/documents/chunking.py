"""切段（T-63／T-64，設計 4.2）：抽取結果的結構段 → 約 400 token、帶重疊的 chunk。

- 先依結構（md／docx 標題區段、pdf 頁、pptx 投影片、整份純文字）；結構段估算不超過
  上限就整段一個 chunk，locator 沿用結構段的。
- 超過上限才在段內定長切：切點優先落在段落（空行）→ 換行 → 句末標點 → 空白／逗號，
  找不到才硬切；相鄰 chunk 重疊約 `overlap_tokens`（預設 50，約 12.5%）。
  locator：`offset` 類改成該 chunk 在段內的起始字元位置（加上段本身的 offset）；
  其他類加 `part`（1 起算）。
- 不設「過小不產生 chunk」的下限：只有全空白的段落被丟掉（短投影片、只有標題的
  段落仍是可檢索內容，B1 裁決「短簡報不可被擋」）。
- `Chunk.overlap`：開頭與前一個 chunk 重疊的字元數。串接全文時略過這段即還原原文。

token 以字元粗估（不呼叫 tokenizer）：CJK 字 1.5 token、其他非空白字元 0.25 token、
空白 0。400 token ≈ 266 個中文字或約 1600 個英文字元。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .extract.base import CJK_CLASS, Segment

DEFAULT_MAX_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 50
CJK_TOKEN_WEIGHT = 1.5
OTHER_TOKEN_WEIGHT = 0.25
# 切點最早可以退到上限的多少比例（避免為了找邊界切出太短的 chunk）
_MIN_FILL = 0.6

_CJK_RE = re.compile(f"[{CJK_CLASS}]")
# 依優先序的切點規則：切在比對結果之後
_BREAKS = (
    re.compile(r"\n[ \t]*\n"),
    re.compile(r"\n"),
    re.compile(r"[。！？；!?;]|[.](?=\s)"),
    re.compile(r"[\s、，,]"),
)


@dataclass(frozen=True)
class Chunk:
    idx: int
    text: str
    locator: dict[str, Any]
    # 開頭與前一個 chunk 重疊的字元數（同一結構段內才會重疊）
    overlap: int = 0


def _weight(ch: str) -> float:
    if ch.isspace():
        return 0.0
    if _CJK_RE.match(ch):
        return CJK_TOKEN_WEIGHT
    return OTHER_TOKEN_WEIGHT


def estimate_tokens(text: str) -> float:
    return sum(_weight(ch) for ch in text)


def _prefix(text: str) -> list[float]:
    sums = [0.0]
    total = 0.0
    for ch in text:
        total += _weight(ch)
        sums.append(total)
    return sums


def _furthest(prefix: list[float], start: int, budget: float) -> int:
    """從 start 起、估算 token 不超過 budget 的最遠結束位置（至少前進一個字元）。"""
    limit = prefix[start] + budget
    lo, hi = start + 1, len(prefix) - 1
    if prefix[lo] > limit:
        return lo
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if prefix[mid] <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _break_point(text: str, prefix: list[float], start: int, end: int) -> int:
    """在 (start, end] 內找最後一個偏好的切點；都沒有就回 end。"""
    if end >= len(text):
        return len(text)
    floor_cost = prefix[start] + (prefix[end] - prefix[start]) * _MIN_FILL
    floor = start + 1
    while floor < end and prefix[floor] < floor_cost:
        floor += 1
    window = text[floor:end]
    for pattern in _BREAKS:
        last = None
        for match in pattern.finditer(window):
            last = match
        if last is not None:
            return floor + last.end()
    return end


def _overlap_start(
    text: str, prefix: list[float], start: int, end: int, overlap: float
) -> int:
    """下一個 chunk 的起點：從 end 往回約 overlap token，並對齊到詞邊界。"""
    if overlap <= 0:
        return end
    target = prefix[end] - overlap
    pos = end
    while pos > start + 1 and prefix[pos - 1] >= target:
        pos -= 1
    # 不要從英文字中間開始：往後挪到下一個空白之後（仍須在 end 之前）
    if pos > 0 and not text[pos - 1].isspace() and not _CJK_RE.match(text[pos]):
        match = re.compile(r"\s").search(text, pos, end)
        if match is not None:
            pos = match.end()
    return min(pos, end)


def split_text(
    text: str,
    *,
    max_tokens: float = DEFAULT_MAX_TOKENS,
    overlap_tokens: float = DEFAULT_OVERLAP_TOKENS,
) -> list[tuple[int, int, int]]:
    """把一段文字切成 [(start, end, overlap)]。不超過上限就整段一個。"""
    if max_tokens <= 0:
        raise ValueError("max_tokens 必須大於 0")
    if overlap_tokens < 0 or overlap_tokens * 2 > max_tokens:
        raise ValueError("overlap_tokens 必須在 0 到 max_tokens 的一半之間")
    prefix = _prefix(text)
    if prefix[-1] <= max_tokens:
        return [(0, len(text), 0)]
    pieces: list[tuple[int, int, int]] = []
    start, overlap = 0, 0
    while True:
        end = _break_point(text, prefix, start, _furthest(prefix, start, max_tokens))
        pieces.append((start, end, overlap))
        if end >= len(text) or not text[end:].strip():
            return pieces
        next_start = _overlap_start(text, prefix, start, end, overlap_tokens)
        if next_start <= start:  # 保證前進
            next_start = end
        overlap = end - next_start
        start = next_start


def chunk_segments(
    segments: Iterable[Segment],
    *,
    max_tokens: float = DEFAULT_MAX_TOKENS,
    overlap_tokens: float = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """結構段 → chunk（idx 從 0 連號）。全空白的段與片段不產生 chunk。"""
    chunks: list[Chunk] = []
    for segment in segments:
        base = segment.locator.to_dict()
        pieces = split_text(
            segment.text, max_tokens=max_tokens, overlap_tokens=overlap_tokens
        )
        multiple = len(pieces) > 1
        for part, (start, end, overlap) in enumerate(pieces, start=1):
            piece = segment.text[start:end]
            if not piece.strip():
                continue
            locator = dict(base)
            if multiple:
                if locator["kind"] == "offset":
                    locator["value"] = int(locator["value"]) + start
                else:
                    locator["part"] = part
            chunks.append(Chunk(len(chunks), piece, locator, overlap))
    return chunks


def join_chunks(chunks: Iterable[tuple[str, int]]) -> str:
    """(text, overlap) 依序串回全文：同段內略過重疊；不同段之間空一行。"""
    out: list[str] = []
    for text, overlap in chunks:
        if overlap:
            out.append(text[overlap:])
        else:
            if out:
                out.append("\n\n")
            out.append(text)
    return "".join(out)
