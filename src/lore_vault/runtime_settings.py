"""執行期設定（D13）：可在 UI 設定頁調整、存 DB 覆寫設定檔／環境變數的白名單。純標準庫。

- 設定檔與環境變數（`config.load_config`）是「預設值」；DB 的 `settings_overrides`
  覆寫其上。只收「執行期安全、非密鑰、不需重啟」的項目，每項都要有讀取端在每次使用時
  取有效值（`api.runtime.RuntimeConfig`），並有測試證明不重建 app 也生效。
- 刻意不收（需重啟或涉及密鑰）：token／密碼／OpenAI key、資料庫與 blob 路徑、模型與
  base_url（用戶端在啟動時建立）、worker 參數與 session 期限（啟動時建構）、
  `documents.max_file_bytes`（MCP 請求大小上限在啟動時決定）等。
- 值只收 JSON 原型別：bool 必須是 true／false（不收 "true"、1），整數不收小數，
  數字不收 NaN／無限大，且都在各自範圍內。
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import Config, ConfigError, validate_config

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"

# (代號, 顯示名稱)；UI 依這個順序分組
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("privacy", "收料與隱私"),
    ("ask", "問答"),
    ("mcp", "MCP"),
    ("health", "健康檢查門檻"),
    ("login", "登入紀錄"),
)
_CATEGORY_IDS = frozenset(c for c, _ in CATEGORIES)


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str
    category: str
    label: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.kind,
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "min": self.minimum,
            "max": self.maximum,
            "unit": self.unit,
        }


SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "episodes.ingest",
        KIND_BOOL,
        "privacy",
        "接收 episode（對話紀錄）",
        "開啟後服務接受各機器 hook 推送的對話紀錄（POST /v1/episodes），"
        "供每日管線蒸餾。關閉時服務拒收，紀錄留在各機器本地、之後開啟會自動補推；"
        "已收進來的紀錄仍可讀取。",
    ),
    SettingSpec(
        "tasks.remote_sync",
        KIND_BOOL,
        "privacy",
        "任務層遠端同步",
        "開啟後服務接受任務層進行中 change 的全文與主規格鏡像（task- 開頭的側載），"
        "讓其他機器與 HTTP MCP 也能操作任務層。關閉時服務拒收這些寫入，任務層只能在"
        "本機操作；已同步的內容仍可讀取。",
    ),
    SettingSpec(
        "ask.enabled",
        KIND_BOOL,
        "ask",
        "啟用問答",
        "關閉時問答（ask）一律回 ask_disabled，不呼叫模型、不產生費用。",
    ),
    SettingSpec(
        "ask.snippet_max_chars",
        KIND_INT,
        "ask",
        "每則筆記送進模型的字數上限",
        "問答時每則筆記正文節錄的上限；越大越完整，費用也越高。",
        minimum=500,
        maximum=50_000,
        unit="字元",
    ),
    SettingSpec(
        "mcp.http_download_max_bytes",
        KIND_INT,
        "mcp",
        "HTTP MCP 下載上限",
        "HTTP MCP 端點的 download 以 base64 回傳原始檔，內容會進 agent 的上下文；"
        "超過上限請改用本機殼或 UI 下載。",
        minimum=1024,
        maximum=25 * 1024 * 1024,
        unit="位元組",
    ),
    SettingSpec(
        "backup.max_age_hours",
        KIND_FLOAT,
        "health",
        "備份新鮮度門檻",
        "最近一次備份超過這麼久（或從未備份）時，健康檢查的備份項目為失敗。",
        minimum=1,
        maximum=24 * 90,
        unit="小時",
    ),
    SettingSpec(
        "documents.stuck_seconds",
        KIND_FLOAT,
        "health",
        "文件抽取卡住門檻",
        "文件停在抽取中超過這麼久時，健康檢查的文件卡住項目為失敗。",
        minimum=60,
        maximum=7 * 86_400,
        unit="秒",
    ),
    SettingSpec(
        "database.tombstone_warn_age_days",
        KIND_FLOAT,
        "health",
        "墓碑年齡警告門檻",
        "最舊的刪除墓碑超過這麼多天時提醒清理；0 表示不提醒。",
        minimum=0,
        maximum=3650,
        unit="天",
    ),
    SettingSpec(
        "database.tombstone_warn_bytes",
        KIND_INT,
        "health",
        "墓碑快照容量警告門檻",
        "刪除墓碑保存的內容快照總量超過時提醒清理；0 表示不提醒。",
        minimum=0,
        maximum=1 << 40,
        unit="位元組",
    ),
    SettingSpec(
        "ui.login_log_retention_days",
        KIND_FLOAT,
        "login",
        "登入紀錄保留天數",
        "登入嘗試紀錄保留的天數；過期的在下一次登入嘗試時清除。",
        minimum=1,
        maximum=3650,
        unit="天",
    ),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SPECS}


def _check_catalog() -> None:
    """白名單自身的一致性：鍵對得上 Config 欄位、型別與預設值相符、分類已登記。"""
    config = Config()
    for spec in SPECS:
        if spec.category not in _CATEGORY_IDS:
            raise AssertionError(f"{spec.key} 的分類 {spec.category!r} 未登記")
        section = getattr(config, spec.section)
        default = getattr(section, spec.name)
        expected = {KIND_BOOL: bool, KIND_INT: int, KIND_FLOAT: float}[spec.kind]
        if type(default) is not expected:
            raise AssertionError(f"{spec.key} 型別與 Config 不符")


_check_catalog()


class InvalidSetting(ValueError):
    """鍵不在白名單（`code="unknown_setting"`）或值不合法（`"invalid_value"`）。"""

    def __init__(self, key: str, code: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.code = code


def spec_for(key: str) -> SettingSpec:
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise InvalidSetting(key, "unknown_setting", f"{key!r} 不是可調整的設定")
    return spec


def validate_value(key: str, value: Any) -> bool | int | float:
    """依白名單驗證並正規化（整數值的 float 轉 int）。不合法拋 `InvalidSetting`。"""
    spec = spec_for(key)

    def bad(message: str) -> InvalidSetting:
        return InvalidSetting(key, "invalid_value", message)

    if spec.kind == KIND_BOOL:
        if not isinstance(value, bool):
            raise bad("必須是 true 或 false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad("必須是數字")
    if isinstance(value, float) and not math.isfinite(value):
        raise bad("必須是有限的數字")
    number: int | float
    if spec.kind == KIND_INT:
        if isinstance(value, float):
            if not value.is_integer():
                raise bad("必須是整數")
            number = int(value)
        else:
            number = value
    else:
        number = float(value)
    if spec.minimum is not None and number < spec.minimum:
        raise bad(f"不可小於 {_fmt(spec.minimum)}")
    if spec.maximum is not None and number > spec.maximum:
        raise bad(f"不可大於 {_fmt(spec.maximum)}")
    return number


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def base_value(config: Config, key: str) -> Any:
    spec = spec_for(key)
    return getattr(getattr(config, spec.section), spec.name)


def apply_overrides(config: Config, overrides: Mapping[str, Any]) -> Config:
    """把（已驗證的）覆寫套到 `config`，再跑設定的跨欄位規則。

    鍵或值不合法拋 `InvalidSetting`；合起來違反 `config.validate_config` 拋
    `ConfigError`。"""
    by_section: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        spec = spec_for(key)
        by_section.setdefault(spec.section, {})[spec.name] = validate_value(key, value)
    changes = {
        section: dataclasses.replace(getattr(config, section), **values)
        for section, values in by_section.items()
    }
    result = dataclasses.replace(config, **changes)
    validate_config(result)
    return result


__all__ = [
    "CATEGORIES",
    "KIND_BOOL",
    "KIND_FLOAT",
    "KIND_INT",
    "SPECS",
    "SPEC_BY_KEY",
    "ConfigError",
    "InvalidSetting",
    "SettingSpec",
    "apply_overrides",
    "base_value",
    "spec_for",
    "validate_value",
]
