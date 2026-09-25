"""設定載入（A10）：環境變數 > 設定檔（TOML）> 預設值。

- 設定檔路徑：`load_config(path=...)` 參數 > 環境變數 `LORE_VAULT_CONFIG` > 不讀檔。
- 環境變數覆寫單一項目：`LORE_VAULT_<SECTION>_<KEY>`，例如
  `LORE_VAULT_EMBEDDING_BASE_URL`、`LORE_VAULT_SUMMARY_MODEL`、`LORE_VAULT_DATABASE_PATH`。
- `.env`：只在呼叫端明確傳 `env_file` 時讀取，合併進「環境變數」這一層
  （真正的環境變數優先），不寫回 `os.environ`。
- OpenAI key 只從環境變數 `OPENAI_API_KEY`（或 `.env`）讀；設定檔裡出現任何
  名稱像密鑰的鍵一律拒絕。key 包在 `Secret`，repr／str 都不會露出內容。

import 本模組無副作用：不讀檔、不讀環境變數。
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any

ENV_PREFIX = "LORE_VAULT_"
CONFIG_PATH_ENV = "LORE_VAULT_CONFIG"
OPENAI_KEY_ENV = "OPENAI_API_KEY"
API_TOKEN_ENV = "LORE_VAULT_API_TOKEN"

# 設定檔中不可出現的鍵（名稱等於或以這些字樣結尾即拒絕）；密鑰只走環境變數。
# 用結尾比對而非包含：`max_completion_tokens` 不是密鑰。
_SECRET_KEY_SUFFIXES = ("key", "apikey", "secret", "token", "password", "passwd")


class ConfigError(ValueError):
    """設定檔或環境變數內容不合法。訊息不含任何密鑰值。"""


class Secret:
    """包住密鑰字串；repr／str 一律遮蔽，只有 `reveal()` 取得原值。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("Secret 不可序列化")


@dataclass(frozen=True)
class DatabaseConfig:
    # 資料目錄尚未定案（D5），不給預設路徑；使用端缺值時明確報錯
    path: str | None = None


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "bge-m3"
    dim: int = 1024
    timeout: float = 30.0
    # 請求路徑（HTTP API 的 recall 與 write 查重）的 embedding 逾時（秒）。
    # 與背景補算的 `timeout` 分開：請求端不能被 Ollama 拖住，逾時即降級。
    query_timeout: float = 3.0
    # 每分鐘呼叫上限；0 = 不限
    rate_per_minute: int = 0


@dataclass(frozen=True)
class SummaryConfig:
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-6-luna"
    # 必須明確設定：不指定時推理會吃光 max_completion_tokens、回空字串（D4）
    reasoning_effort: str = "low"
    max_completion_tokens: int = 1000
    timeout: float = 60.0
    rate_per_minute: int = 30


@dataclass(frozen=True)
class WorkerConfig:
    # 同一則 note、同一版本最多嘗試幾次，超過標記失敗
    max_attempts: int = 3
    # 單次執行最多處理幾則 note（每種補算各自計）
    batch_size: int = 50
    # 失敗後至少等多久才重試（秒；第 n 次失敗等 base * 2^(n-1)）
    retry_backoff: float = 60.0
    # 常駐模式兩輪之間的間隔（秒）
    poll_interval: float = 30.0


@dataclass(frozen=True)
class ApiConfig:
    # HTTP 服務是否在同一程序內以背景執行緒跑補算 worker（A9：單一寫入程序）
    enrich_worker: bool = True


@dataclass(frozen=True)
class BackupConfig:
    # 備份目錄（容器內預設 /backups，由主機 bind mount；只放備份檔、不放 live DB）
    dir: str | None = None
    # 保留最近幾份
    keep: int = 7
    # doctor「最近一次備份」門檻（小時）；超過或從未備份為 fail
    max_age_hours: float = 26.0


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


_SECTIONS: dict[str, type] = {
    "database": DatabaseConfig,
    "embedding": EmbeddingConfig,
    "summary": SummaryConfig,
    "worker": WorkerConfig,
    "backup": BackupConfig,
    "api": ApiConfig,
}

# 布林設定可接受的寫法（環境變數是字串；TOML 可直接寫 true／false）
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _looks_secret(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered.endswith(_SECRET_KEY_SUFFIXES)


def _coerce(section: str, key: str, value: Any, default: Any, source: str) -> Any:
    """依預設值型別轉換；錯誤訊息只帶鍵名與來源，不帶值（避免誤貼密鑰外洩）。"""
    where = f"{source} 的 {section}.{key}"
    if default is None or isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} 必須是字串")
        return value
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
        raise ConfigError(f"{where} 必須是布林值（true／false／1／0）")
    if isinstance(default, int):
        if isinstance(value, bool):
            raise ConfigError(f"{where} 必須是整數")
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} 必須是整數") from None
        if isinstance(value, float) and value != result:
            raise ConfigError(f"{where} 必須是整數")
        return result
    if isinstance(default, float):
        if isinstance(value, bool):
            raise ConfigError(f"{where} 必須是數字")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} 必須是數字") from None
    raise ConfigError(f"{where} 型別不支援")


def _read_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"設定檔不存在：{path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"設定檔不是合法 TOML：{path}（{exc}）") from None
    for section, values in data.items():
        if _looks_secret(section):
            raise ConfigError(f"設定檔不可包含密鑰（{section}）；請改用環境變數")
        if section not in _SECTIONS:
            raise ConfigError(f"設定檔有未知區段 [{section}]：{path}")
        if not isinstance(values, dict):
            raise ConfigError(f"設定檔的 {section} 必須是表格：{path}")
        for key in values:
            if _looks_secret(key):
                raise ConfigError(
                    f"設定檔不可包含密鑰（{section}.{key}）；"
                    f"OpenAI key 請放環境變數 {OPENAI_KEY_ENV} 或 .env"
                )
    return data


def _merged_environ(
    environ: Mapping[str, str], env_file: str | PathLike[str] | None
) -> dict[str, str]:
    merged: dict[str, str] = {}
    if env_file is not None:
        from dotenv import dotenv_values

        path = Path(env_file)
        if not path.is_file():
            raise ConfigError(f".env 檔不存在：{path}")
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(environ)
    return merged


def load_config(
    path: str | PathLike[str] | None = None,
    *,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """載入設定。`environ` 預設為 `os.environ`（測試可注入）。"""
    env = _merged_environ(os.environ if environ is None else environ, env_file)
    if path is None and env.get(CONFIG_PATH_ENV):
        path = env[CONFIG_PATH_ENV]
    file_data = _read_file(Path(path)) if path is not None else {}

    sections: dict[str, Any] = {}
    for section, cls in _SECTIONS.items():
        defaults = cls()
        known = {f.name for f in dataclasses.fields(cls)}
        from_file = file_data.get(section, {})
        unknown = sorted(set(from_file) - known)
        if unknown:
            raise ConfigError(f"設定檔 [{section}] 有未知項目 {unknown}")
        values: dict[str, Any] = {}
        for name in known:
            default = getattr(defaults, name)
            env_name = f"{ENV_PREFIX}{section.upper()}_{name.upper()}"
            if env_name in env:
                values[name] = _coerce(section, name, env[env_name], default, env_name)
            elif name in from_file:
                values[name] = _coerce(
                    section, name, from_file[name], default, "設定檔"
                )
        sections[section] = dataclasses.replace(defaults, **values)
    config = Config(**sections)
    _validate(config)
    return config


def _validate(config: Config) -> None:
    if config.embedding.provider != "ollama":
        raise ConfigError(f"不支援的 embedding provider：{config.embedding.provider}")
    if config.summary.provider != "openai":
        raise ConfigError(f"不支援的摘要 provider：{config.summary.provider}")
    if not config.summary.reasoning_effort.strip():
        # 不指定 effort 會回空字串（D4），不允許清空
        raise ConfigError("summary.reasoning_effort 不可為空")
    positive = {
        "embedding.dim": config.embedding.dim,
        "embedding.timeout": config.embedding.timeout,
        "embedding.query_timeout": config.embedding.query_timeout,
        "summary.max_completion_tokens": config.summary.max_completion_tokens,
        "summary.timeout": config.summary.timeout,
        "worker.max_attempts": config.worker.max_attempts,
        "worker.batch_size": config.worker.batch_size,
        "worker.poll_interval": config.worker.poll_interval,
        "backup.keep": config.backup.keep,
        "backup.max_age_hours": config.backup.max_age_hours,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ConfigError(f"{name} 必須大於 0")
    non_negative = {
        "embedding.rate_per_minute": config.embedding.rate_per_minute,
        "summary.rate_per_minute": config.summary.rate_per_minute,
        "worker.retry_backoff": config.worker.retry_backoff,
    }
    for name, value in non_negative.items():
        if value < 0:
            raise ConfigError(f"{name} 不可為負")


def openai_api_key(
    *,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Secret | None:
    """從環境變數（或 `.env`）取 OpenAI key；沒有或空白時回 None。"""
    env = _merged_environ(os.environ if environ is None else environ, env_file)
    value = env.get(OPENAI_KEY_ENV, "").strip()
    return Secret(value) if value else None


def api_token(
    *,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Secret | None:
    """HTTP API 的 bearer token（A15），只從環境變數 `LORE_VAULT_API_TOKEN`
    （或 `.env`）讀；沒有或空白時回 None。長度等規則由 API 層檢查。"""
    env = _merged_environ(os.environ if environ is None else environ, env_file)
    value = env.get(API_TOKEN_ENV, "").strip()
    return Secret(value) if value else None
