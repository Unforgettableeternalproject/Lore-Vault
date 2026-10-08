"""HTTP 服務進入點：`uvicorn --factory lore_vault.api.app:create_app`。

啟動順序（lifespan）：遷移資料庫 → 背景 embedding 暖機（不等它完成）→
啟動背景補算 worker 與文件 worker（各自可關閉；文件 worker 需 documents.blob_dir）；
關閉時（uvicorn 收到 SIGTERM／SIGINT → lifespan 結束）停止 worker 並等它結束。
token 不合格時 `create_app` 直接拋 `ConfigError`，服務不會啟動；正式啟動路徑
（`load_settings`）在 `LORE_VAULT_API_TOKEN` 未設時沿用或產生資料目錄的
`secrets/api-token`（D12）。

啟動時另清除過期的 UI 登入紀錄（A23）；正式啟動路徑在資料庫沒有任何 UI 帳號時
建立管理員（D12，`api.bootstrap`）。

MCP（D12）：`/mcp` 是 Streamable HTTP MCP 端點（`mcp.http`），與 stdio 殼共用工具
定義，經 in-process ASGI 轉發到本 app 的 `/v1/*`；認證與 `/v1/*` 相同。

UI（A21）：`/ui/api/*` 登入端點一律掛上；`ui.static_dir` 有設定時在 `/ui` 提供
Vite 建置後的靜態檔（SPA fallback 到 index.html）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from lore_vault.mcp.http import MCP_PATH, build_http_endpoint
from lore_vault.storage import ui_login

from .auth import BearerAuthMiddleware
from .bootstrap import ensure_admin_account
from .errors import install_error_handlers
from .manage import router as manage_router
from .principals import Principals
from .routes import router
from .settings import ApiSettings, load_settings, validate_token
from .settings_admin import router as settings_router
from .spike import router as spike_router
from .state import AppState
from .tasks_admin import router as tasks_router
from .ui import UiSecurityHeadersMiddleware, static_app
from .ui import build_router as build_ui_router
from .ui_auth import UiAuth


def _configure_logging() -> None:
    """讓 lore_vault.* 的 INFO log 出現在 stdout（docker logs）。

    uvicorn 只設定自己的 logger，root 預設 WARNING 會吞掉補算進度與暖機結果。
    只在 lore_vault logger 尚無 handler 時加一個，避免重複輸出。
    """
    root = logging.getLogger("lore_vault")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def _purge_login_log(state: AppState, ui_auth: UiAuth) -> None:
    """啟動時清除過期的 UI 登入紀錄（A23；每次登入嘗試也會順手清）。"""
    now = datetime.fromtimestamp(ui_auth.clock(), UTC)
    retention = state.runtime.current().ui.login_log_retention_days
    with state.connection() as conn:
        removed = ui_login.purge_log(conn, now=now, retention_days=retention)
    if removed:
        logging.getLogger("lore_vault.api.ui").info("清除過期登入紀錄 %d 筆", removed)


def _bootstrap_admin(state: AppState, ui_auth: UiAuth) -> None:
    """D12：資料庫沒有任何 UI 帳號時建立管理員；已有帳號完全不動。"""
    settings = state.settings
    if not settings.bootstrap_admin:
        return
    with state.connection() as conn:
        ensure_admin_account(
            conn,
            username=settings.resolved_admin_user,
            password=settings.admin_password,
            secrets_dir=settings.resolved_secrets_dir,
            now=datetime.fromtimestamp(ui_auth.clock(), UTC),
        )


def create_app(
    settings: ApiSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """建立 app。`settings` 省略時從環境變數載入（`environ` 供測試注入）。"""
    if settings is None:
        # 正式啟動路徑（uvicorn --factory）；測試會注入 settings，不改動全域 logging
        _configure_logging()
        settings = load_settings(environ=environ)
    validate_token(settings.token)
    # 憑證 → principal（A22）：目前唯一的 token 對應設定的 principal（D12）
    principals = Principals.single(settings.token, settings.principal)
    ui_auth = UiAuth.from_config(settings.config.ui, settings.clock or time.time)
    ui_static = (
        static_app(settings.config.ui.static_dir)
        if settings.config.ui.static_dir
        else None
    )
    state = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state.migrate()
        _bootstrap_admin(state, ui_auth)
        _purge_login_log(state, ui_auth)
        state.warmup.start()
        if state.enricher is not None:
            state.enricher.start()
        if state.documents_worker is not None:
            state.documents_worker.start()
        try:
            async with mcp_endpoint.run():
                yield
        finally:
            state.warmup.stop()
            if state.enricher is not None:
                state.enricher.stop()
            if state.documents_worker is not None:
                state.documents_worker.stop()
            state.close()

    app = FastAPI(
        title="Lore Vault",
        version="0.1.1",
        lifespan=lifespan,
        # 契約文件放在需認證的 /v1 底下；不提供互動式文件頁
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.lore = state
    app.state.ui_auth = ui_auth
    # MCP 工具經 in-process ASGI 打回本 app（含認證中介層），所以傳 app 本身
    mcp_endpoint = build_http_endpoint(app, settings)
    app.router.add_route(MCP_PATH, mcp_endpoint, include_in_schema=False)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(spike_router)
    app.include_router(manage_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    # /ui/api/* 必須在 /ui 靜態掛載之前註冊（Starlette 依註冊順序比對）
    app.include_router(build_ui_router())
    if ui_static is not None:
        app.mount("/ui", ui_static, name="ui")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """存活檢查：不需認證、不碰資料庫與任何資料。"""
        return JSONResponse({"ok": True})

    app.add_middleware(
        BearerAuthMiddleware,
        token=settings.token,
        ui_auth=ui_auth,
        principals=principals,
    )
    # 最後加入 = 最外層：/ui 的所有回應（含認證中介層產生的）都帶安全標頭
    app.add_middleware(UiSecurityHeadersMiddleware)
    return app
