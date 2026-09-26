"""HTTP 服務進入點：`uvicorn --factory lore_vault.api.app:create_app`。

啟動順序（lifespan）：遷移資料庫 → 背景 embedding 暖機（不等它完成）→
啟動背景補算 worker（可關閉）；
關閉時（uvicorn 收到 SIGTERM／SIGINT → lifespan 結束）停止 worker 並等它結束。
token 缺少或不合格時 `create_app` 直接拋 `ConfigError`，服務不會啟動。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .auth import BearerAuthMiddleware
from .errors import install_error_handlers
from .routes import router
from .settings import ApiSettings, load_settings, validate_token
from .spike import router as spike_router
from .state import AppState


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
    state = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state.migrate()
        state.warmup.start()
        if state.enricher is not None:
            state.enricher.start()
        try:
            yield
        finally:
            if state.enricher is not None:
                state.enricher.stop()
            state.close()

    app = FastAPI(
        title="Lore Vault",
        version="0.1.0",
        lifespan=lifespan,
        # 契約文件放在需認證的 /v1 底下；不提供互動式文件頁
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.lore = state
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(spike_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """存活檢查：不需認證、不碰資料庫與任何資料。"""
        return JSONResponse({"ok": True})

    app.add_middleware(BearerAuthMiddleware, token=settings.token)
    return app
