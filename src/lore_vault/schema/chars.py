"""控制字元與孤立 surrogate 的單一定義：驗證（拒收）與清理（替換成可見形式）。

純標準庫、無 import 副作用：hook（系統 Python 直接執行）、儲存層、匯入工具共用這裡。

為什麼要擋：舊 PM（Open Notebook）有一則 note 內文夾了真正的 NUL 位元組，整則永久
讀不出來，還讓全量列表端點 500。SQLite 對含 NUL 的 TEXT，`length()`、`LIKE`、
`substr()` 都在 NUL 處截斷、JSON1 函式直接判為不合法（實測見
tests/storage/test_control_chars.py），所以 NUL 一旦進庫，SQL 端的行為就不可信。

三層防護：
- 寫入路徑（notes 的 write／update）：`check_text`／`check_fields` → `InvalidCharacters`
  明確拒收，不回顯內容，只指出欄位與第一個位置
- 不可拒收的收料（ON 匯入、episode）：`sanitize_text`／`sanitize_value` 換成
  可見字元並計數
- doctor `storage.control_chars`：掃描已入庫的資料

禁用字元：C0 控制字元（U+0000–U+001F）中除了 `\\t` `\\n` `\\r` 以外的 29 個，以及孤立
surrogate（U+D800–U+DFFF；Python `str` 可以有，但無法編碼成 UTF-8，sqlite3 寫入時會拋
`UnicodeEncodeError`）。DEL（U+007F）與 C1 不在範圍內。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ._base import SchemaError

ALLOWED_CONTROL = frozenset("\t\n\r")
FORBIDDEN_CONTROL = frozenset(
    chr(code) for code in range(0x20) if chr(code) not in ALLOWED_CONTROL
)

KIND_CONTROL = "control"
KIND_SURROGATE = "surrogate"

# 清理後的可見形式：NUL → 兩字元 `\0`；其他 C0 → `\xNN`；孤立 surrogate → `\uXXXX`
NUL_REPLACEMENT = "\\0"


class InvalidCharacters(SchemaError):
    """欄位含禁用的控制字元或孤立 surrogate。訊息只含欄位名、位置與碼位，不含內容。"""

    def __init__(self, field: str, index: int, codepoint: int, kind: str) -> None:
        self.field = field
        self.index = index
        self.codepoint = codepoint
        self.kind = kind
        what = "控制字元" if kind == KIND_CONTROL else "孤立 surrogate"
        super().__init__(
            f"{field}: 第 {index} 個字元（0 起算）是不允許的{what} U+{codepoint:04X}"
        )


def _bad_kind(ch: str) -> str | None:
    if ch in FORBIDDEN_CONTROL:
        return KIND_CONTROL
    if "\ud800" <= ch <= "\udfff":
        return KIND_SURROGATE
    return None


def find_invalid(text: str) -> tuple[int, str] | None:
    """第一個禁用字元的 (字元索引, 種類)；沒有回 None。"""
    for index, ch in enumerate(text):
        kind = _bad_kind(ch)
        if kind is not None:
            return index, kind
    return None


def has_invalid(text: str) -> bool:
    return find_invalid(text) is not None


def check_text(field: str, value: Any) -> None:
    """字串含禁用字元就拋 `InvalidCharacters`。

    非字串（None 等）不管，型別由 schema 驗。"""
    if not isinstance(value, str):
        return
    found = find_invalid(value)
    if found is not None:
        index, kind = found
        raise InvalidCharacters(field, index, ord(value[index]), kind)


def check_fields(fields: Mapping[str, Any]) -> None:
    """逐欄檢查：字串直接查；字串清單（topics、links）逐項查，欄位名帶索引。"""
    for name, value in fields.items():
        if isinstance(value, str) or value is None:
            check_text(name, value)
        elif isinstance(value, Iterable) and not isinstance(value, Mapping):
            for index, item in enumerate(value):
                check_text(f"{name}[{index}]", item)


def _replacement(ch: str) -> str:
    if ch == "\x00":
        return NUL_REPLACEMENT
    code = ord(ch)
    if code < 0x20:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}"


def sanitize_text(text: str) -> tuple[str, int]:
    """把禁用字元換成可見形式；回傳 (結果, 替換數)。冪等：結果再清理不會變。"""
    if find_invalid(text) is None:
        return text, 0
    out: list[str] = []
    count = 0
    for ch in text:
        if _bad_kind(ch) is not None:
            out.append(_replacement(ch))
            count += 1
        else:
            out.append(ch)
    return "".join(out), count


def sanitize_value(value: Any) -> tuple[Any, int]:
    """遞迴清理 JSON 形狀的值（dict 的鍵與值、list／tuple 的項、字串）。

    回傳 (新值, 替換數)。
    不修改原物件；沒有需要替換時原樣回傳同一個物件。
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        total = 0
        result: dict[Any, Any] = {}
        for key, item in value.items():
            new_key, n_key = sanitize_value(key)
            new_item, n_item = sanitize_value(item)
            total += n_key + n_item
            result[new_key] = new_item
        return (result, total) if total else (value, 0)
    if isinstance(value, (list, tuple)):
        total = 0
        items = []
        for item in value:
            new_item, n = sanitize_value(item)
            total += n
            items.append(new_item)
        if not total:
            return value, 0
        return (tuple(items) if isinstance(value, tuple) else items), total
    return value, 0
