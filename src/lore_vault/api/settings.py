"""HTTP 服務的啟動設定：資料庫路徑、bearer token（A15）、embedder 與 worker 注入點。

`load_settings()` 從環境變數（與可選的設定檔、`.env`）組出設定；
token 缺少或太短時 `create_app` 拒絕啟動，不會默默以無認證狀態開放。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING

from lore_vault.config import (
    API_TOKEN_ENV,
    Config,
    ConfigError,
    Secret,
    api_token,
    load_config,
    openai_api_key,
)
from lore_vault.enrich.clients import Transport
from lore_vault.enrich.worker import EnrichWorker
from lore_vault.recall.embedder import Embedder

if TYPE_CHECKING:
    from lore_vault.ask.client import Answerer
    from lore_vault.documents.worker import DocumentWorker

# token 最短長度：擋掉 "test"、"1234" 這類一看就猜得到的值
MIN_TOKEN_LENGTH = 16

# (背景執行緒自己的連線, should_stop) -> worker
WorkerFactory = Callable[[sqlite3.Connection, Callable[[], bool]], EnrichWorker]
# 文件 worker 的建構方式（同上；預設 documents.worker.build_document_worker）
DocumentWorkerFactory = Callable[
    [sqlite3.Connection, Callable[[], bool]], "DocumentWorker"
]


@dataclass(frozen=True)
class ApiSettings:
    db_path: Path
    token: Secret
    config: Config = field(default_factory=Config)
    # 補算摘要用；None 時 worker 略過摘要（與命令列行為一致）
    openai_key: Secret | None = None
    # ── 測試注入點；None 表示依 config 建立 ──
    # recall 與 write 查重用的 embedder（預設 Ollama、逾時 embedding.query_timeout）
    query_embedder: Embedder | None = None
    # 背景 worker 的建構方式（預設 enrich.command.build_worker）
    worker_factory: WorkerFactory | None = None
    # 覆寫 config.api.enrich_worker
    enrich_worker: bool | None = None
    # 關閉時等 worker 執行緒結束的秒數（進行中的 HTTP 呼叫可能還要一段時間）
    worker_join_timeout: float = 5.0
    # Ollama HTTP transport（預設 urllib）；查詢 embedder 與暖機 embedder 都用它
    embed_transport: Transport | None = None
    # 覆寫 config.api.embedding_warmup
    embedding_warmup: bool | None = None
    # 快照快取目錄（`GET /v1/snapshot`）；None = 啟動後在系統暫存目錄建一個，關閉時刪除
    snapshot_cache_dir: Path | None = None
    # 文件 worker 的建構方式（預設 documents.worker.build_document_worker）
    document_worker_factory: DocumentWorkerFactory | None = None
    # 覆寫 config.api.document_worker
    document_worker: bool | None = None
    # UI session 與登入限流用的時鐘（epoch 秒；None = time.time）
    clock: Callable[[], float] | None = None
    # `/v1/ask` 的問答模型（D11）；None = 依 config.ask 與 openai_key 建立
    # （沒有 key 時 ask 回 `ask_not_configured`）
    answerer: Answerer | None = None
    # 問答模型的 HTTP transport（預設 urllib）；只在 answerer 為 None 時使用
    llm_transport: Transport | None = None

    @property
    def run_worker(self) -> bool:
        if self.enrich_worker is not None:
            return self.enrich_worker
        return self.config.api.enrich_worker

    @property
    def run_document_worker(self) -> bool:
        """blob_dir 未設定時文件功能整個關閉，worker 不啟動。"""
        if not self.config.documents.blob_dir:
            return False
        if self.document_worker is not None:
            return self.document_worker
        return self.config.api.document_worker

    @property
    def run_warmup(self) -> bool:
        if self.embedding_warmup is not None:
            return self.embedding_warmup
        return self.config.api.embedding_warmup


def validate_token(token: Secret | None) -> Secret:
    """缺少、太短或含空白的 token 一律拒絕；訊息不含 token 內容。"""
    if token is None or not token.reveal():
        raise ConfigError(
            f"未設定 {API_TOKEN_ENV}，拒絕啟動（HTTP API 不可無認證開放）"
        )
    value = token.reveal()
    if len(value) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"{API_TOKEN_ENV} 太短，至少 {MIN_TOKEN_LENGTH} 個字元")
    if any(ch.isspace() for ch in value):
        raise ConfigError(f"{API_TOKEN_ENV} 不可包含空白字元")
    return token


def load_settings(
    *,
    config_path: str | PathLike[str] | None = None,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApiSettings:
    """從環境變數組設定。資料庫路徑取 `database.path`（`LORE_VAULT_DATABASE_PATH`）。"""
    config = load_config(config_path, env_file=env_file, environ=environ)
    token = validate_token(api_token(env_file=env_file, environ=environ))
    if not config.database.path:
        raise ConfigError(
            "缺少資料庫路徑：設定 database.path（環境變數 LORE_VAULT_DATABASE_PATH）"
        )
    return ApiSettings(
        db_path=Path(config.database.path),
        token=token,
        config=config,
        openai_key=openai_api_key(env_file=env_file, environ=environ),
    )
