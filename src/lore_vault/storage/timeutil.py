"""時間戳格式（T-23）：寫入端一律 `YYYY-MM-DDTHH:MM:SS.sssZ`。

- 與 spike episode 現有格式相同（2026-09-26 抽樣 6165 筆全為此格式），
  整個系統只有一種字串形狀，字串排序 = 時間排序。
- `Note.updated` 同時是樂觀鎖版本，比對的是字串本身；因此寫入時先正規化，
  之後讀出、比對都用資料庫裡的那個字串，不在讀取端重新序列化。
- 精度到毫秒；SurrealDB 匯入的奈秒精度會截斷（同一毫秒內的兩次更新由
  `next_after` 往後推 1ms，版本仍嚴格遞增）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lore_vault.schema import SchemaError

_FORMAT_EXAMPLE = "2026-01-01T00:00:00.000Z"


def format_utc(moment: datetime) -> str:
    """把帶時區的 datetime 轉成標準字串；naive datetime 直接拒絕（不猜時區）。"""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SchemaError(f"時間缺少時區，無法轉成 UTC：{moment!r}")
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def utc_now() -> str:
    return format_utc(datetime.now(UTC))


def parse_utc(value: str) -> datetime:
    """解析 ISO-8601；必須明確是 UTC（`Z` 或 `+00:00`）。"""
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"時間戳必須是非空字串：{value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError(f"不是 ISO-8601 時間：{value!r}") from exc
    if parsed.tzinfo is None:
        raise SchemaError(f"缺少時區，必須是 UTC：{value!r}")
    if parsed.utcoffset() != timedelta(0):
        raise SchemaError(f"時區必須是 UTC：{value!r}")
    return parsed


def normalize_utc(value: str) -> str:
    """驗證並轉成標準字串。已是標準形狀的字串原樣回傳。"""
    return format_utc(parse_utc(value))


def normalize_opt_utc(value: str | None) -> str | None:
    return None if value is None else normalize_utc(value)


def next_after(previous: str, candidate: str | None = None) -> str:
    """回傳嚴格晚於 `previous` 的時間戳（預設用現在時間）。

    時鐘回撥或同一毫秒內連續更新時，往後推 1ms，確保樂觀鎖版本不重複。
    """
    proposed = parse_utc(candidate if candidate is not None else utc_now())
    floor = parse_utc(previous) + timedelta(milliseconds=1)
    return format_utc(max(proposed, floor))
