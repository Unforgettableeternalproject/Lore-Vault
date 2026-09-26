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
CF_ACCESS_ID_ENV = "CF_ACCESS_CLIENT_ID"
CF_ACCESS_SECRET_ENV = "CF_ACCESS_CLIENT_SECRET"

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
    # doctor `tombstones.summary`（資訊項）的警告門檻；0 = 不警告（預設）。
    # 墓碑與內容快照永久保留，只由 `cli.admin purge-tombstones` 明確清除
    tombstone_warn_age_days: float = 0.0
    tombstone_warn_bytes: int = 0


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
    # Ollama `keep_alive`：請求後模型留在記憶體多久（Go duration，如 "30m"；
    # 純整數視為秒數，負值＝常駐）。空字串＝不送，交給 Ollama 預設（5m）。
    # 冷啟動載入約 2 秒，逼近 query_timeout，所以預設拉長避免閒置後被卸載。
    keep_alive: str = "30m"


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
    # 服務啟動後在背景做一次 embedding 暖機（載入 Ollama 模型）；
    # 不阻擋啟動、失敗只記 log
    embedding_warmup: bool = True
    # 同程序的文件 worker（抽取 → 切段 → 索引 → chunk 向量補算）；
    # documents.blob_dir 未設定時不啟動
    document_worker: bool = True


@dataclass(frozen=True)
class BackupConfig:
    # 備份目錄（容器內預設 /backups，由主機 bind mount；只放備份檔、不放 live DB）
    dir: str | None = None
    # 保留最近幾份
    keep: int = 7
    # doctor「最近一次備份」門檻（小時）；超過或從未備份為 fail
    max_age_hours: float = 26.0


@dataclass(frozen=True)
class McpConfig:
    """各機器本地 MCP 殼（A15）：轉發服務 HTTP、拉快照、不可達時降級。"""

    # 服務位址（本機 docker 為 127.0.0.1:5056；遠端走 Cloudflare 子網域）
    base_url: str = "http://127.0.0.1:5056"
    # 每個 HTTP 請求的逾時（秒）；逾時視為服務不可達
    timeout: float = 10.0
    # 本地唯讀快照目錄；未設定 = 不拉快照、服務不可達時無法降級
    snapshot_dir: str | None = None
    # 定期拉快照的間隔（秒）；0 = 只在殼啟動時拉一次
    snapshot_interval: float = 900.0
    # doctor「快照年齡」門檻（小時）
    snapshot_max_age_hours: float = 24.0
    # Cloudflare Access service token 的 env 檔（格式同 ~/.cloudflared/pm-token.env）
    cf_access_env_file: str | None = None
    # PreToolUse 讀的 concept 快照檔（T-40）；未設定時為
    # `<snapshot_dir>/concepts.json`，snapshot_dir 也未設定就不拉。
    # 刻意不預設成 spike 現行的 concepts.json（切換時再改指向）
    concept_snapshot_path: str | None = None
    # `upload` 工具可讀取的額外目錄（以 os.pathsep 分隔，Windows 為 ';'）。
    # 殼的工作目錄一律在白名單內（A19 D10-6）；殼能讀到其下任何檔案
    upload_roots: str | None = None


@dataclass(frozen=True)
class DocumentsConfig:
    """文件存儲與抽取（A19、T-58～T-62）。"""

    # blob（原始檔，內容定址）目錄；容器內為 named volume 下的 /data/blobs。
    # 未設定 = 不能收文件（使用端缺值時明確報錯），doctor 的 blob 對帳記為 skipped
    blob_dir: str | None = None
    # 單檔原始大小上限（位元組），預設 25MB
    max_file_bytes: int = 25 * 1024 * 1024
    # 抽出文字總長上限（字元），超過標 too_large
    max_chars: int = 10_000_000
    # pdf 抽出文字去空白後少於此字數標 empty_extraction（多半是掃描件；B1 裁決只套 pdf）
    min_chars: int = 50
    # 切段（設計 4.2）：每個 chunk 的估算 token 上限與相鄰 chunk 的重疊 token 數。
    # token 以字元粗估（`documents.chunking.estimate_tokens`），不是實際 tokenizer。
    # 只影響之後抽取的文件；已索引的文件不重切
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 50
    # 卡在 extracting 超過此秒數 doctor `documents.stuck_processing` 為 fail
    stuck_seconds: float = 3600.0
    # pdf／docx／pptx 在子行程抽取（`documents.isolation`）：超過此秒數 kill 子行程、
    # 標 corrupt（detail 註明 timeout）。從子行程就緒起算，不含啟動時間
    extract_timeout: float = 60.0
    # 抽取子行程的虛擬記憶體上限（MB，RLIMIT_AS；只在 Linux 生效，Windows 無此能力）。
    # 0 = 不限制。Python + lxml + pypdf 本身就佔數百 MB 位址空間，不要設太低
    extract_memory_mb: int = 1024


@dataclass(frozen=True)
class UiConfig:
    """使用者 UI（A21）：靜態檔位置與本地身分驗證（session cookie、帳號密碼登入）。

    登入改用 DB 內的 UI 帳號密碼（A23，`storage.ui_login`）：全域失敗 3 次即鎖定、
    需人工解鎖；規則固定，不做設定項。
    """

    # Vite 建置產物（index.html 所在目錄）；未設定 = 不提供 /ui 靜態檔
    # （/ui/api/* 仍可用，供 Vite dev server proxy）。容器內為 /app/ui
    static_dir: str | None = None
    # session cookie 帶 Secure（名稱加 __Host- 前綴）。只有本機 http 開發才關閉；
    # 瀏覽器對 http://localhost 視為安全來源，預設值在本機多半也能用
    cookie_secure: bool = True
    # session 絕對期限（小時）：登入後最多這麼久，不因使用而延長
    session_absolute_hours: float = 12.0
    # session 閒置期限（分鐘）：超過這麼久沒有認證請求即失效
    session_idle_minutes: float = 60.0
    # 同時存在的 session 上限；超過時淘汰最舊的
    max_sessions: int = 32
    # 登入紀錄（`ui_login_log`）保留天數；過期的在服務啟動與每次登入嘗試時清除
    login_log_retention_days: float = 90.0
    # 受信任代理（逗號分隔的 IP 或 CIDR）。只有直接連線來源在清單內時才採信
    # `CF-Connecting-IP` 當作用戶端 IP（登入紀錄的來源）；空 = 一律用直接連線來源
    trusted_proxies: str = ""


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    documents: DocumentsConfig = field(default_factory=DocumentsConfig)
    ui: UiConfig = field(default_factory=UiConfig)


_SECTIONS: dict[str, type] = {
    "database": DatabaseConfig,
    "embedding": EmbeddingConfig,
    "summary": SummaryConfig,
    "worker": WorkerConfig,
    "backup": BackupConfig,
    "api": ApiConfig,
    "mcp": McpConfig,
    "documents": DocumentsConfig,
    "ui": UiConfig,
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
        "mcp.timeout": config.mcp.timeout,
        "mcp.snapshot_max_age_hours": config.mcp.snapshot_max_age_hours,
        "documents.max_file_bytes": config.documents.max_file_bytes,
        "documents.max_chars": config.documents.max_chars,
        "documents.chunk_max_tokens": config.documents.chunk_max_tokens,
        "documents.stuck_seconds": config.documents.stuck_seconds,
        "documents.extract_timeout": config.documents.extract_timeout,
        "ui.session_absolute_hours": config.ui.session_absolute_hours,
        "ui.session_idle_minutes": config.ui.session_idle_minutes,
        "ui.max_sessions": config.ui.max_sessions,
        "ui.login_log_retention_days": config.ui.login_log_retention_days,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ConfigError(f"{name} 必須大於 0")
    non_negative = {
        "embedding.rate_per_minute": config.embedding.rate_per_minute,
        "summary.rate_per_minute": config.summary.rate_per_minute,
        "worker.retry_backoff": config.worker.retry_backoff,
        "mcp.snapshot_interval": config.mcp.snapshot_interval,
        "documents.min_chars": config.documents.min_chars,
        "documents.chunk_overlap_tokens": config.documents.chunk_overlap_tokens,
        "documents.extract_memory_mb": config.documents.extract_memory_mb,
        "database.tombstone_warn_age_days": config.database.tombstone_warn_age_days,
        "database.tombstone_warn_bytes": config.database.tombstone_warn_bytes,
    }
    for name, value in non_negative.items():
        if value < 0:
            raise ConfigError(f"{name} 不可為負")
    if config.documents.chunk_overlap_tokens * 2 > config.documents.chunk_max_tokens:
        raise ConfigError(
            "documents.chunk_overlap_tokens 不可超過 chunk_max_tokens 的一半"
        )
    if not config.mcp.base_url.startswith(("http://", "https://")):
        raise ConfigError("mcp.base_url 必須以 http:// 或 https:// 開頭")


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


def cf_access_credentials(
    *,
    cf_env_file: str | PathLike[str] | None = None,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Secret, Secret] | None:
    """Cloudflare Access service token（`CF_ACCESS_CLIENT_ID`／`..._SECRET`）。

    來源優先序：環境變數 > `env_file`（`.env`）> `cf_env_file`（如
    `~/.cloudflared/pm-token.env`）。兩者都沒有回 None（本機直連不需要）；
    只有其中一個時拋 `ConfigError`（半套設定不可默默略過）。訊息不含值。
    """
    merged: dict[str, str] = {}
    if cf_env_file is not None:
        merged.update(_merged_environ({}, cf_env_file))
    merged.update(_merged_environ(os.environ if environ is None else environ, env_file))
    client_id = merged.get(CF_ACCESS_ID_ENV, "").strip()
    secret = merged.get(CF_ACCESS_SECRET_ENV, "").strip()
    if not client_id and not secret:
        return None
    if not client_id or not secret:
        missing = CF_ACCESS_ID_ENV if not client_id else CF_ACCESS_SECRET_ENV
        raise ConfigError(f"Cloudflare Access 設定不完整：缺少 {missing}")
    return Secret(client_id), Secret(secret)
