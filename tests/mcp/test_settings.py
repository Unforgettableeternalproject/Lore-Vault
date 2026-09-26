"""殼設定：優先序、Cloudflare Access 憑證來源、密鑰不外洩。"""

from __future__ import annotations

import logging

import httpx2
import pytest

from lore_vault.config import ConfigError
from lore_vault.mcp.settings import load_shell_settings

from .conftest import CF_ID, CF_SECRET, TOKEN, failing, make_shell

BASE_ENV = {"LORE_VAULT_API_TOKEN": TOKEN}


def test_defaults_and_env_override(tmp_path):
    settings = load_shell_settings(environ=BASE_ENV)
    assert settings.base_url == "http://127.0.0.1:5056"
    assert settings.cf_access is None
    assert settings.snapshot_dir is None

    settings = load_shell_settings(
        environ={
            **BASE_ENV,
            "LORE_VAULT_MCP_BASE_URL": "https://pm-api.example.com/",
            "LORE_VAULT_MCP_SNAPSHOT_DIR": str(tmp_path),
            "LORE_VAULT_MCP_SNAPSHOT_INTERVAL": "0",
            "LORE_VAULT_MCP_TIMEOUT": "3",
        }
    )
    assert settings.base_url == "https://pm-api.example.com"
    assert settings.snapshot_dir == tmp_path
    assert settings.snapshot_interval == 0
    assert settings.timeout == 3.0


def test_config_file_section(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp]\nbase_url = "http://10.0.0.2:5056"\nsnapshot_max_age_hours = 6.0\n',
        encoding="utf-8",
    )
    settings = load_shell_settings(config_path=cfg, environ=BASE_ENV)
    assert settings.base_url == "http://10.0.0.2:5056"
    assert settings.snapshot_max_age_hours == 6.0
    # 環境變數優先於設定檔
    settings = load_shell_settings(
        config_path=cfg,
        environ={**BASE_ENV, "LORE_VAULT_MCP_BASE_URL": "http://127.0.0.1:1"},
    )
    assert settings.base_url == "http://127.0.0.1:1"


def test_token_required_and_not_in_config_file(tmp_path):
    with pytest.raises(ConfigError, match="LORE_VAULT_API_TOKEN"):
        load_shell_settings(environ={})
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp]\napi_token = "leak"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="密鑰"):
        load_shell_settings(config_path=cfg, environ=BASE_ENV)


def test_base_url_must_be_http():
    with pytest.raises(ConfigError, match="base_url"):
        load_shell_settings(environ={**BASE_ENV, "LORE_VAULT_MCP_BASE_URL": "ftp://x"})


def test_cf_access_from_token_file(tmp_path):
    # 格式同 ~/.cloudflared/pm-token.env
    token_file = tmp_path / "pm-token.env"
    token_file.write_text(
        f"CF_ACCESS_CLIENT_ID={CF_ID}\nCF_ACCESS_CLIENT_SECRET={CF_SECRET}\n",
        encoding="utf-8",
    )
    settings = load_shell_settings(
        environ={**BASE_ENV, "LORE_VAULT_MCP_CF_ACCESS_ENV_FILE": str(token_file)}
    )
    assert settings.cf_access is not None
    assert [s.reveal() for s in settings.cf_access] == [CF_ID, CF_SECRET]
    # 環境變數優先
    settings = load_shell_settings(
        environ={
            **BASE_ENV,
            "LORE_VAULT_MCP_CF_ACCESS_ENV_FILE": str(token_file),
            "CF_ACCESS_CLIENT_ID": "other-id",
        }
    )
    assert settings.cf_access[0].reveal() == "other-id"
    # repr 不露值
    text = repr(settings)
    assert CF_SECRET not in text and TOKEN not in text


def test_half_cf_config_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="CF_ACCESS_CLIENT_SECRET") as info:
        load_shell_settings(environ={**BASE_ENV, "CF_ACCESS_CLIENT_ID": CF_ID})
    assert CF_ID not in str(info.value)
    missing = tmp_path / "nope.env"
    with pytest.raises(ConfigError, match="不存在"):
        load_shell_settings(
            environ={**BASE_ENV, "LORE_VAULT_MCP_CF_ACCESS_ENV_FILE": str(missing)}
        )


def test_env_file_supplies_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LORE_VAULT_API_TOKEN={TOKEN}\nCF_ACCESS_CLIENT_ID={CF_ID}\n"
        f"CF_ACCESS_CLIENT_SECRET={CF_SECRET}\n",
        encoding="utf-8",
    )
    settings = load_shell_settings(env_file=env_file, environ={})
    assert settings.token.reveal() == TOKEN
    assert settings.cf_access is not None


@pytest.mark.anyio
async def test_secrets_never_logged(tmp_path, caplog, anyio_backend):
    from lore_vault.config import Secret

    shell = make_shell(
        failing(lambda r: httpx2.ConnectError("refused", request=r)),
        tmp_path / "snap",
        cf_access=(Secret(CF_ID), Secret(CF_SECRET)),
    )
    with caplog.at_level(logging.DEBUG):
        assert await shell.refresh_snapshot() is None
    await shell.aclose()
    text = caplog.text + (shell.last_pull_error or "") + repr(shell.client)
    assert TOKEN not in text and CF_SECRET not in text and CF_ID not in text
