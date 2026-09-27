"""D12：首次啟動（API token、UI 管理員）與 principal 設定。

- 不讀 os.environ：一律以 `environ=` 注入；資料目錄在 tmp_path
- 只在 load_settings 路徑測 token（`create_app(environ=...)` 會設定全域 logging，
  這裡避開，log 以掛在 bootstrap logger 的 handler 收）
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from lore_vault.api import bootstrap
from lore_vault.api.app import create_app
from lore_vault.api.settings import load_settings
from lore_vault.config import ConfigError, Secret, configured_principal
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.schema import DEFAULT_PRINCIPAL
from lore_vault.storage import ui_login
from lore_vault.storage.db import connect

from .conftest import CHEAP_SCRYPT, TOKEN, create_vault, make_settings

NOW = datetime(2026, 9, 27, tzinfo=UTC)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def boot_log():
    logger = logging.getLogger("lore_vault.api.bootstrap")
    handler = _Records()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _env(tmp_path, **extra) -> dict[str, str]:
    return {"LORE_VAULT_DATABASE_PATH": str(tmp_path / "data" / "lore.db"), **extra}


# ── API token ────────────────────────────────────────────────────────


def test_token_generated_once_then_reused(tmp_path, boot_log):
    first = load_settings(environ=_env(tmp_path))
    path = tmp_path / "data" / "secrets" / "api-token"
    token = first.token.reveal()
    assert path.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 32
    assert len(boot_log) == 1 and token in boot_log[0] and str(path) in boot_log[0]
    second = load_settings(environ=_env(tmp_path))
    assert second.token.reveal() == token
    assert len(boot_log) == 1  # 沿用時不再印


@pytest.mark.skipif(os.name == "nt", reason="POSIX 才 chmod")
def test_generated_secret_files_are_0600(tmp_path):
    settings = load_settings(environ=_env(tmp_path))
    secrets_dir = settings.resolved_secrets_dir
    mode = stat.S_IMODE((secrets_dir / "api-token").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    conn = connect(settings.db_path)
    try:
        bootstrap.ensure_admin_account(
            conn,
            username="owner",
            password=None,
            secrets_dir=secrets_dir,
            now=NOW,
            params=CHEAP_SCRYPT,
        )
    finally:
        conn.close()
    admin = secrets_dir / "initial-admin-password"
    assert stat.S_IMODE(admin.stat().st_mode) == 0o600


def test_env_token_wins_and_writes_nothing(tmp_path, boot_log):
    settings = load_settings(environ=_env(tmp_path, LORE_VAULT_API_TOKEN=TOKEN))
    assert settings.token.reveal() == TOKEN
    assert not (tmp_path / "data" / "secrets").exists()
    assert boot_log == []


def test_existing_token_file_is_used(tmp_path):
    secrets_dir = tmp_path / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "api-token").write_text("file-token-0123456789\n", encoding="utf-8")
    assert load_settings(environ=_env(tmp_path)).token.reveal() == (
        "file-token-0123456789"
    )


def test_weak_token_file_refuses_to_start(tmp_path):
    secrets_dir = tmp_path / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "api-token").write_text("short", encoding="utf-8")
    with pytest.raises(ConfigError, match="太短"):
        load_settings(environ=_env(tmp_path))


def test_concurrent_creator_result_is_read(tmp_path, monkeypatch):
    """O_EXCL 輸給另一個行程時讀對方寫好的檔，而不是覆蓋。"""
    secrets_dir = tmp_path / "secrets"
    real = bootstrap._create_secret_file

    def lose_race(path, value):
        real(path, "winner-token-0123456789")
        return real(path, value)  # 檔案已存在 → False

    monkeypatch.setattr(bootstrap, "_create_secret_file", lose_race)
    token = bootstrap.ensure_api_token(None, secrets_dir)
    assert token.reveal() == "winner-token-0123456789"


# ── principal ────────────────────────────────────────────────────────


def test_principal_default_and_env(tmp_path):
    assert DEFAULT_PRINCIPAL == "owner"
    assert configured_principal(environ={}) == "owner"
    assert configured_principal(environ={"LORE_VAULT_PRINCIPAL": " "}) == "owner"
    assert (
        configured_principal(environ={"LORE_VAULT_PRINCIPAL": "UEPBernie"})
        == "UEPBernie"
    )
    with pytest.raises(ConfigError, match="LORE_VAULT_PRINCIPAL"):
        configured_principal(environ={"LORE_VAULT_PRINCIPAL": "has space"})
    settings = load_settings(
        environ=_env(tmp_path, LORE_VAULT_API_TOKEN=TOKEN, LORE_VAULT_PRINCIPAL="me")
    )
    assert settings.principal == "me" and settings.resolved_admin_user == "me"


# ── UI 管理員 ────────────────────────────────────────────────────────


def _accounts(db_path):
    conn = connect(db_path)
    try:
        return [(a.username, a.updated) for a in ui_login.list_accounts(conn)]
    finally:
        conn.close()


def test_admin_created_once_with_generated_password(tmp_path, db_path, boot_log):
    secrets_dir = tmp_path / "secrets"
    settings = make_settings(
        db_path, bootstrap_admin=True, principal="me", secrets_dir=secrets_dir
    )
    with TestClient(create_app(settings)):
        pass
    password_file = secrets_dir / "initial-admin-password"
    password = password_file.read_text(encoding="utf-8").strip()
    assert [u for u, _ in _accounts(db_path)] == ["me"]
    conn = connect(db_path)
    try:
        assert ui_login.attempt_login(conn, "me", password, ip="t", now=NOW).ok
    finally:
        conn.close()
    assert len(boot_log) == 1 and password in boot_log[0]
    before = _accounts(db_path)
    # 再次啟動：已有帳號，完全不動（不改密碼、不重寫檔、不再印）
    password_file.write_text("kept\n", encoding="utf-8")
    with TestClient(create_app(settings)):
        pass
    assert _accounts(db_path) == before
    assert password_file.read_text(encoding="utf-8") == "kept\n"
    assert len(boot_log) == 1


def test_admin_from_env_writes_no_file(tmp_path, db_path, boot_log):
    secrets_dir = tmp_path / "secrets"
    settings = make_settings(
        db_path,
        bootstrap_admin=True,
        admin_user="boss",
        admin_password=Secret("given password 123"),
        secrets_dir=secrets_dir,
    )
    with TestClient(create_app(settings)):
        pass
    assert [u for u, _ in _accounts(db_path)] == ["boss"]
    assert not (secrets_dir / "initial-admin-password").exists()
    assert all("given password 123" not in m for m in boot_log)


def test_existing_account_is_untouched(tmp_path, db_path):
    from .conftest import seed_ui_account

    seed_ui_account(db_path, username="someone")
    before = _accounts(db_path)
    settings = make_settings(
        db_path,
        bootstrap_admin=True,
        admin_password=Secret("would change 123"),
        secrets_dir=tmp_path / "secrets",
    )
    with TestClient(create_app(settings)):
        pass
    assert _accounts(db_path) == before


def test_injected_settings_do_not_bootstrap_admin(db_path):
    """測試直接建構的設定預設不建管理員（避免憑空多一個帳號）。"""
    with TestClient(create_app(make_settings(db_path))):
        pass
    assert _accounts(db_path) == []


def test_invalid_admin_password_refuses_to_start(tmp_path, db_path):
    settings = make_settings(
        db_path,
        bootstrap_admin=True,
        admin_password=Secret("short"),
        secrets_dir=tmp_path / "secrets",
    )
    with pytest.raises(ConfigError, match="LORE_VAULT_ADMIN_PASSWORD"):
        with TestClient(create_app(settings)):
            pass


# ── doctor：principal 與既有 note 一致 ───────────────────────────────


def _principal_check(db_path, principal):
    conn = connect(db_path)
    try:
        report = default_registry().run(
            DoctorContext(settings={"principal": principal}, resources={"db": conn}),
            categories=["notes"],
        )
    finally:
        conn.close()
    (check,) = [
        o.to_dict() for o in report.outcomes if o.name == "notes.principal_agreement"
    ]
    return check


def test_doctor_principal_agreement(db_path, make_client):
    connect(db_path).close()
    assert _principal_check(db_path, "owner")["status"] == "pass"  # 沒有 note
    c = make_client(principal="UEPBernie")
    create_vault(c, "v")
    c.post("/v1/write", json={"vault": "v", "title": "t", "body": "b"})
    assert _principal_check(db_path, "UEPBernie")["status"] == "pass"
    mismatch = _principal_check(db_path, "owner")
    assert mismatch["status"] == "warn"
    assert "UEPBernie" in mismatch["summary"]
    assert any("LORE_VAULT_PRINCIPAL=UEPBernie" in d for d in mismatch["details"])
    assert _principal_check(db_path, None)["status"] == "skipped"


def test_status_runs_principal_check_with_service_setting(db_path, make_client):
    """/v1/status 必須把服務的 principal 交給 doctor；漏傳時這項會變 skipped。"""
    writer = make_client(principal="UEPBernie")
    create_vault(writer, "v")
    writer.post("/v1/write", json={"vault": "v", "title": "t", "body": "b"})
    # 同一個資料庫、漏設 LORE_VAULT_PRINCIPAL（預設 owner）的服務
    fresh = make_client()
    checks = fresh.post("/v1/status").json()["doctor"]["checks"]
    (check,) = [c for c in checks if c["name"] == "notes.principal_agreement"]
    assert check["status"] == "warn"
    assert "owner" in check["summary"] and "UEPBernie" in check["summary"]
