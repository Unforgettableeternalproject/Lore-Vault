"""list 篩選共用的 SQL 片段：子字串比對（LIKE 跳脫）與清單參數驗證。

note 與文件的 list／count 都從同一組 conditions 組 SQL，分頁與總數才會一致。
純標準庫（hook 與 doctor 也可能 import 儲存層）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# 子字串篩選的長度上限：篩選值不是正文，過長多半是誤用
MAX_FILTER_TEXT = 200
_EXTENSION = re.compile(r"^[a-z0-9]{1,16}$")


def filter_text(name: str, value: str | None) -> str | None:
    """子字串篩選值：None 不過濾；去頭尾空白後不可為空、不可超過上限。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} 必須是字串")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} 不可為空字串；不過濾請傳 None")
    if len(text) > MAX_FILTER_TEXT:
        raise ValueError(f"{name} 不可超過 {MAX_FILTER_TEXT} 字")
    return text


def contains_clause(column: str, text: str) -> tuple[str, str]:
    """`column` 含 `text`（ASCII 不分大小寫；`%`、`_`、`\\` 照字面比對）。"""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{column} LIKE ? ESCAPE '\\'", f"%{escaped}%"


def filter_choices(
    name: str, values: Sequence[str] | None, allowed: Sequence[str]
) -> tuple[str, ...] | None:
    """列舉清單篩選：None 不過濾；不可為空清單、不可有白名單外的值。"""
    if values is None:
        return None
    if isinstance(values, str):
        raise TypeError(f"{name} 必須是清單，不可傳單一字串")
    picked = tuple(dict.fromkeys(values))
    if not picked:
        raise ValueError(f"{name} 不可為空清單；不過濾請傳 None")
    unknown = sorted(set(picked) - set(allowed))
    if unknown:
        raise ValueError(f"未知的 {name}：{unknown}；可用 {list(allowed)}")
    return picked


def filter_extensions(values: Sequence[str] | None) -> tuple[str, ...] | None:
    """副檔名清單（不含點、比對時不分大小寫）：None 不過濾。"""
    if values is None:
        return None
    if isinstance(values, str):
        raise TypeError("extensions 必須是清單，不可傳單一字串")
    picked = tuple(dict.fromkeys(v.strip().lower().lstrip(".") for v in values))
    if not picked:
        raise ValueError("extensions 不可為空清單；不過濾請傳 None")
    bad = [v for v in picked if not _EXTENSION.match(v)]
    if bad:
        raise ValueError(f"副檔名只能是英數字（不含點）：{bad}")
    return picked
