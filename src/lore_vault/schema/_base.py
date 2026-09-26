"""schema 共用的驗證工具與序列化基底（純標準庫、無 import 副作用）。

設計重點：
- 未知欄位、缺必填欄位一律拋 `SchemaError`，不靜默吞掉或補預設值。
- 「值為 None」與「欄位不存在」要分得開：後者以 `MISSING` 哨兵表示，
  `to_dict` 時整個鍵省略。spike 的 scope 事故（`or` 把 None 壓成 repo 名）
  就是這兩者混淆。
- 時間一律 ISO-8601 且明確為 UTC；只驗證、不改寫原字串（凍結欄位不在讀取時重算）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, ClassVar, Self


class SchemaError(ValueError):
    """資料不符合 schema：未知欄位、缺必填、型別或值不合法。"""


class _Missing:
    """「欄位不存在」的哨兵，與 None（明確填了空值）區分。"""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        # 防止被當成假值與 None 混用；需要判斷時一律用 `is MISSING`
        raise TypeError("MISSING 不可當布林值使用，請用 `is MISSING` 判斷")

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self


MISSING = _Missing()


def fail(owner: str, field: str, message: str) -> SchemaError:
    return SchemaError(f"{owner}.{field}: {message}")


def req_str(owner: str, field: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise fail(owner, field, f"必須是字串，得到 {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise fail(owner, field, "不可為空字串")
    return value


def opt_str(owner: str, field: str, value: Any, *, allow_empty: bool = False) -> None:
    if value is not None:
        req_str(owner, field, value, allow_empty=allow_empty)


def req_int(owner: str, field: str, value: Any, *, minimum: int | None = 0) -> int:
    # bool 是 int 的子類，要排除
    if isinstance(value, bool) or not isinstance(value, int):
        raise fail(owner, field, f"必須是整數，得到 {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise fail(owner, field, f"不可小於 {minimum}，得到 {value}")
    return value


def req_bool(owner: str, field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise fail(owner, field, f"必須是布林值，得到 {type(value).__name__}")
    return value


def str_tuple(
    owner: str, field: str, value: Any, *, allow_empty_items: bool = False
) -> tuple[str, ...]:
    """接受 list/tuple of str，回傳 tuple；字串本身不算序列（避免被逐字拆開）。"""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise fail(owner, field, f"必須是字串清單，得到 {type(value).__name__}")
    for index, item in enumerate(value):
        req_str(owner, f"{field}[{index}]", item, allow_empty=allow_empty_items)
    return tuple(value)


def opt_mapping(owner: str, field: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise fail(owner, field, f"必須是物件或 null，得到 {type(value).__name__}")
    return dict(value)


def utc_timestamp(owner: str, field: str, value: Any) -> str:
    """驗證 ISO-8601 且時區明確為 UTC（`Z` 或 `+00:00`）；回傳原字串不改寫。

    沒有時區的時間戳直接拒絕——容器是 UTC、主機是 +08:00，
    猜時區等於把錯誤凍結進資料。
    """
    req_str(owner, field, value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise fail(owner, field, f"不是 ISO-8601 時間：{value!r}") from exc
    if parsed.tzinfo is None:
        raise fail(owner, field, f"缺少時區，必須是 UTC：{value!r}")
    if parsed.utcoffset() != timedelta(0):
        raise fail(owner, field, f"時區必須是 UTC，得到 {value!r}")
    return value


def opt_utc_timestamp(owner: str, field: str, value: Any) -> None:
    if value is not None:
        utc_timestamp(owner, field, value)


def set_field(obj: Any, name: str, value: Any) -> None:
    """在 frozen dataclass 的 `__post_init__` 內正規化欄位（list → tuple）。"""
    object.__setattr__(obj, name, value)


def _to_plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict()  # type: ignore[attr-defined]
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


class Record:
    """五個型別共用的 to_dict／from_dict。子類必須是 dataclass。

    - `REQUIRED`：from_dict 時鍵必須存在的欄位
      （值可否為 None 由 `__post_init__` 決定）。
    - 預設值為 `MISSING` 的欄位：鍵不存在時保持 `MISSING`，to_dict 省略該鍵。
    """

    REQUIRED: ClassVar[frozenset[str]] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if value is MISSING:
                continue
            result[f.name] = _to_plain(value)
        return result

    @classmethod
    def _convert(cls, data: dict[str, Any]) -> dict[str, Any]:
        """子類覆寫：把巢狀的 dict 轉成對應型別。預設不轉換。"""
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        name = cls.__name__
        if not isinstance(data, Mapping):
            raise SchemaError(f"{name}: 必須是物件，得到 {type(data).__name__}")
        known = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        unknown = sorted(set(data) - known)
        if unknown:
            raise SchemaError(f"{name}: 未知欄位 {unknown}")
        missing = sorted(cls.REQUIRED - set(data))
        if missing:
            raise SchemaError(f"{name}: 缺少必填欄位 {missing}")
        return cls(**cls._convert(dict(data)))


def check_required_declared(cls: type, required: Iterable[str]) -> None:
    """防呆：REQUIRED 裡的名字必須是真的欄位（打錯字會讓必填檢查形同虛設）。"""
    known = {f.name for f in dataclasses.fields(cls)}
    stray = sorted(set(required) - known)
    if stray:
        raise TypeError(f"{cls.__name__}.REQUIRED 含不存在的欄位 {stray}")
    # 沒有預設值的欄位漏列時，from_dict 會丟出 TypeError 而非 SchemaError
    no_default = {
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    unlisted = sorted(no_default - set(required))
    if unlisted:
        raise TypeError(f"{cls.__name__}.REQUIRED 漏列無預設值的欄位 {unlisted}")
