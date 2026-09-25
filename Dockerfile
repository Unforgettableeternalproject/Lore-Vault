# syntax=docker/dockerfile:1
# Lore Vault 服務映像（T-26）。
#   建置：docker build -t lore-vault:local .
#   啟動：docker compose up -d（見 docker-compose.yml；資料用 named volume）
# 基底與開發環境同為 Python 3.14（.python-version）；Debian trixie 的 SQLite 3.46，
# 啟動前仍以 lore_vault.storage.sqlite_check 斷言 ≥ 3.37 + FTS5 + STRICT。

ARG PYTHON_IMAGE=python:3.14-slim-trixie

# ── 建置階段：uv 依 uv.lock 安裝相依套件到 /app/.venv ──
FROM ${PYTHON_IMAGE} AS builder

# 版本與 pyproject 的 uv_build<0.10 對齊，不用 :latest（可重現）
COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 第一層：只有相依套件（pyproject／uv.lock 不變就吃快取）
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 第二層：專案本身（非 editable，執行階段不需要原始碼目錄）
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── 執行階段 ──
FROM ${PYTHON_IMAGE} AS runtime

# tini 當 PID 1：轉送 SIGTERM 給 uvicorn、回收殭屍程序（T-01 結論）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# 非 root 使用者；/data、/backups 的擁有者在 USER 之前設好，
# named volume 首次建立時會沿用映像內目錄的擁有者
RUN groupadd --system --gid 10001 lorevault \
    && useradd --system --uid 10001 --gid lorevault --home-dir /app --no-create-home lorevault \
    && mkdir -p /data /backups \
    && chown lorevault:lorevault /data /backups

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY docker/config.toml /app/config.toml

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LORE_VAULT_CONFIG=/app/config.toml

# 建置時就斷言一次：SQLite 能力不足直接讓 build 失敗
RUN python -m lore_vault.storage.sqlite_check

USER lorevault
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
# 啟動前再斷言一次（執行環境的 SQLite 才是真正會用到的），失敗即以非 0 結束；
# exec 讓 uvicorn 取代 sh，直接接收 tini 轉送的 SIGTERM
CMD ["sh", "-c", "python -m lore_vault.storage.sqlite_check && exec uvicorn --factory lore_vault.api.app:create_app --host 0.0.0.0 --port 8000"]
