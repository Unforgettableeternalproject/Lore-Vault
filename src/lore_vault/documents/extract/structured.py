"""結構化資料抽取器：json／yaml／toml（T-60，設計 4.1）。

結構轉成「鍵路徑: 值」的可讀文字後索引（不把原始檔當純文字塞給 FTS，巢狀語意
才不會消失），整份一個 segment、locator `offset 0`。

- 路徑：物件鍵以 `.` 串接、陣列元素 `[i]`（`servers[0].host: example.com`）；
  根是純量時只有值。空物件／空陣列輸出 `{}`／`[]`。
- yaml 用 safe loader（不建構任意物件），多文件（`---`）依序接續，中間空一行。
- 攤平邊累加邊檢查字數上限：YAML 錨點／別名可以把幾 KB 展開成 GB，超過即 too_large；
  自我參照（循環別名）與過深巢狀 → corrupt。
"""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from typing import Any

import yaml

from .base import CORRUPT, Budget, ExtractionError, Locator, Segment, decode_text

MAX_DEPTH = 200

try:  # libyaml 版本快很多；沒有時退回純 Python
    _YamlLoader: Any = yaml.CSafeLoader
except AttributeError:  # pragma: no cover - 視平台 wheel 而定
    _YamlLoader = yaml.SafeLoader


def _render(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    return str(value)


def _flatten(value: Any, budget: Budget) -> list[str]:
    lines: list[str] = []
    active: set[int] = set()

    def emit(line: str) -> None:
        lines.append(budget.add(line + "\n"))

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ExtractionError(CORRUPT, f"巢狀超過 {MAX_DEPTH} 層")
        if isinstance(node, dict | list):
            if id(node) in active:
                raise ExtractionError(CORRUPT, f"自我參照的結構（{path or '根'}）")
            if not node:
                emit(f"{path}: {'{}' if isinstance(node, dict) else '[]'}")
                return
            active.add(id(node))
            if isinstance(node, dict):
                for key, child in node.items():
                    name = _render(key)
                    walk(child, f"{path}.{name}" if path else name, depth + 1)
            else:
                for index, child in enumerate(node):
                    walk(child, f"{path}[{index}]", depth + 1)
            active.discard(id(node))
            return
        emit(f"{path}: {_render(node)}" if path else _render(node))

    walk(value, "", 0)
    return [line.rstrip("\n") for line in lines]


def _single(lines: list[str]) -> list[Segment]:
    text = "\n".join(lines)
    if not text.strip():
        return []
    return [Segment(text, Locator("offset", 0))]


def extract_json(data: bytes, budget: Budget) -> list[Segment]:
    text = decode_text(data, budget)
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ExtractionError(CORRUPT, f"不是合法 JSON：{exc}") from None
    return _single(_flatten(value, budget))


def extract_toml(data: bytes, budget: Budget) -> list[Segment]:
    text = decode_text(data, budget)
    try:
        value = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ExtractionError(CORRUPT, f"不是合法 TOML：{exc}") from None
    return _single(_flatten(value, budget))


def extract_yaml(data: bytes, budget: Budget) -> list[Segment]:
    text = decode_text(data, budget)
    lines: list[str] = []
    try:
        for index, document in enumerate(yaml.load_all(text, Loader=_YamlLoader)):
            if document is None:
                continue
            if index and lines:
                lines.append(budget.add(""))
            lines.extend(_flatten(document, budget))
    except (yaml.YAMLError, RecursionError) as exc:
        raise ExtractionError(CORRUPT, f"不是合法 YAML：{exc}") from None
    return _single(lines)
