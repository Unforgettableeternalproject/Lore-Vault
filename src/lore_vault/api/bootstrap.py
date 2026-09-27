"""首次啟動（D12）：env 優先，缺少就自動產生，產生的密鑰只在產生當次印進 log。

- API token：`LORE_VAULT_API_TOKEN` 有值就用它、不寫檔；未設時讀
  `<資料目錄>/secrets/api-token`，檔案不存在才產生並寫入（0600，POSIX 才 chmod）
- UI 管理員：資料庫**沒有任何** UI 帳號時才建立，帳號 `LORE_VAULT_ADMIN_USER`
  （預設同 principal）、密碼 `LORE_VAULT_ADMIN_PASSWORD`；未給密碼就產生一次性密碼
  寫進 `<資料目錄>/secrets/initial-admin-password`（0600）。已有帳號時完全不動
- 資料目錄 = 資料庫檔所在目錄（容器內 `/data/lore.db` → `/data/secrets/`）

寫檔用 `O_CREAT | O_EXCL`：兩個行程同時首次啟動時只有一個會產生，另一個讀它的結果。
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from lore_vault.config import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USER_ENV,
    API_TOKEN_ENV,
    PRINCIPAL_ENV,
    ConfigError,
    Secret,
)
from lore_vault.storage import ui_login

logger = logging.getLogger("lore_vault.api.bootstrap")

SECRETS_DIRNAME = "secrets"
API_TOKEN_FILE = "api-token"
ADMIN_PASSWORD_FILE = "initial-admin-password"


def secrets_dir_for(db_path: Path) -> Path:
    """密鑰目錄：與資料庫同一個資料目錄（named volume）下的 `secrets/`。"""
    return Path(db_path).parent / SECRETS_DIRNAME


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _create_secret_file(path: Path, value: str) -> bool:
    """新建密鑰檔（0600）；檔案已存在時不動並回 False。"""
    _ensure_dir(path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if os.name != "nt":
        os.chmod(path, 0o600)
    return True


def _replace_secret_file(path: Path, value: str) -> None:
    """寫入（覆蓋）密鑰檔：先寫同目錄暫存檔（0600）再原子替換。"""
    _ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    if not _create_secret_file(tmp, value):  # pragma: no cover - 亂數撞名
        raise OSError(f"暫存檔已存在：{tmp}")
    os.replace(tmp, path)


def ensure_api_token(env_token: Secret | None, secrets_dir: Path) -> Secret:
    """回傳服務 token：env 優先；否則讀檔，檔案不存在才產生（log 只在產生當次印）。"""
    if env_token is not None:
        return env_token
    path = secrets_dir / API_TOKEN_FILE
    if not path.exists():
        token = secrets.token_urlsafe(32)
        if _create_secret_file(path, token):
            logger.warning(
                "未設定 %s：已產生 API token 並存於 %s（0600）。"
                "此值只在這次啟動顯示：%s",
                API_TOKEN_ENV,
                path,
                token,
            )
            return Secret(token)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"無法讀取 API token 檔 {path}：{exc}") from None
    if not value:
        raise ConfigError(
            f"API token 檔 {path} 是空的；刪除它讓服務重新產生，或設定 {API_TOKEN_ENV}"
        )
    return Secret(value)


def ensure_admin_account(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: Secret | None,
    secrets_dir: Path,
    now: datetime,
    params: ui_login.ScryptParams = ui_login.DEFAULT_PARAMS,
) -> str | None:
    """資料庫沒有任何 UI 帳號時建立管理員；回傳建立的 username，已有帳號回 None。"""
    if ui_login.has_accounts(conn):
        return None
    value = secrets.token_urlsafe(18) if password is None else password.reveal()
    try:
        account, _ = ui_login.set_password(
            conn, username, value, now=now, params=params
        )
    except ui_login.AccountError as exc:
        raise ConfigError(
            f"無法建立 UI 管理員（檢查 {ADMIN_USER_ENV}／{PRINCIPAL_ENV}"
            f"／{ADMIN_PASSWORD_ENV}）：{exc}"
        ) from None
    if password is None:
        path = secrets_dir / ADMIN_PASSWORD_FILE
        _replace_secret_file(path, value)
        logger.warning(
            "已建立 UI 管理員 %s；一次性密碼存於 %s（0600），登入後請用 "
            "cli.admin ui-set-password 更換。此值只在這次啟動顯示：%s",
            account.username,
            path,
            value,
        )
    else:
        logger.info(
            "已建立 UI 管理員 %s（密碼取自 %s）", account.username, ADMIN_PASSWORD_ENV
        )
    return account.username
