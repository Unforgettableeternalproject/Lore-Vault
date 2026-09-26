"""設定載入：優先序、型別、密鑰不進設定檔／repr／例外、import 無副作用。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lore_vault.config import (
    Config,
    ConfigError,
    Secret,
    load_config,
    openai_api_key,
)

SENTINEL = "sk-sentinel-DO-NOT-LEAK-42"


def test_defaults():
    cfg = load_config(environ={})
    assert cfg.database.path is None
    assert cfg.embedding.provider == "ollama"
    assert cfg.embedding.base_url == "http://localhost:11434"
    assert cfg.embedding.model == "bge-m3"
    assert cfg.embedding.dim == 1024
    assert cfg.summary.provider == "openai"
    assert cfg.summary.model == "gpt-6-luna"
    assert cfg.summary.reasoning_effort == "low"
    assert cfg.summary.max_completion_tokens > 0
    assert cfg.summary.rate_per_minute > 0
    assert cfg.worker.max_attempts > 0


def test_example_config_loads_and_matches_defaults():
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    assert load_config(example, environ={}) == Config()


def test_priority_env_over_file_over_default(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[embedding]\nmodel = "from-file"\ndim = 8\n[summary]\ntimeout = 5\n',
        encoding="utf-8",
    )
    cfg = load_config(path, environ={"LORE_VAULT_EMBEDDING_MODEL": "from-env"})
    assert cfg.embedding.model == "from-env"  # 環境變數 > 檔案
    assert cfg.embedding.dim == 8  # 檔案 > 預設
    assert cfg.summary.timeout == 5.0 and isinstance(cfg.summary.timeout, float)
    assert cfg.embedding.base_url == "http://localhost:11434"  # 預設


def test_config_path_from_env(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[database]\npath = "x.db"\n', encoding="utf-8")
    cfg = load_config(environ={"LORE_VAULT_CONFIG": str(path)})
    assert cfg.database.path == "x.db"


def test_env_values_are_coerced_and_validated():
    cfg = load_config(environ={"LORE_VAULT_WORKER_MAX_ATTEMPTS": "5"})
    assert cfg.worker.max_attempts == 5
    with pytest.raises(ConfigError, match="整數"):
        load_config(environ={"LORE_VAULT_WORKER_MAX_ATTEMPTS": "many"})
    with pytest.raises(ConfigError, match="大於 0"):
        load_config(environ={"LORE_VAULT_EMBEDDING_DIM": "0"})


def test_empty_reasoning_effort_is_refused():
    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_config(environ={"LORE_VAULT_SUMMARY_REASONING_EFFORT": "  "})


@pytest.mark.parametrize("key", ["api_key", "openai_api_key", "token", "secret"])
def test_secret_in_config_file_is_refused_without_echoing_value(tmp_path, key):
    path = tmp_path / "c.toml"
    path.write_text(f'[summary]\n{key} = "{SENTINEL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path, environ={})
    assert SENTINEL not in str(info.value)
    assert "密鑰" in str(info.value)


def test_unknown_section_and_key_are_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[nope]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未知區段"):
        load_config(path, environ={})
    path.write_text("[summary]\nmodle = 'typo'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未知項目"):
        load_config(path, environ={})


def test_type_error_message_has_no_value(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'[embedding]\ndim = "{SENTINEL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path, environ={})
    assert SENTINEL not in str(info.value)


def test_api_key_from_env_file_without_touching_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"OPENAI_API_KEY={SENTINEL}\nLORE_VAULT_SUMMARY_MODEL=m-env\n",
        encoding="utf-8",
    )
    key = openai_api_key(env_file=env_file, environ={})
    assert isinstance(key, Secret) and key.reveal() == SENTINEL
    assert load_config(env_file=env_file, environ={}).summary.model == "m-env"
    assert "OPENAI_API_KEY" not in os.environ
    # 真正的環境變數優先於 .env
    real = openai_api_key(env_file=env_file, environ={"OPENAI_API_KEY": "real"})
    assert real is not None and real.reveal() == "real"


def test_missing_or_blank_key_is_none():
    assert openai_api_key(environ={}) is None
    assert openai_api_key(environ={"OPENAI_API_KEY": "  "}) is None


def test_secret_never_shows_in_repr_or_str():
    secret = Secret(SENTINEL)
    assert SENTINEL not in repr(secret)
    assert SENTINEL not in str(secret)
    assert SENTINEL not in f"{secret}"
    assert SENTINEL not in repr([secret])
    assert SENTINEL not in repr(load_config(environ={"OPENAI_API_KEY": SENTINEL}))


def test_import_has_no_side_effects(tmp_path):
    """import 不讀 .env、不讀設定：cwd 放哨兵 .env，import 後環境變數沒被塞。"""
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={SENTINEL}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, lore_vault.config, lore_vault.enrich; "
            "print(os.environ.get('OPENAI_API_KEY', 'absent'))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "absent"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


# ── HTTP API 相關設定（T-24／T-28）──────────────────────────────────


def test_api_defaults_and_query_timeout():
    cfg = load_config(environ={})
    assert cfg.api.enrich_worker is True
    assert cfg.embedding.query_timeout == 3.0
    assert cfg.embedding.timeout == 30.0  # 補算用的長逾時不受影響


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("YES", True), ("off", False), ("0", False)],
)
def test_bool_setting_from_env(raw, expected):
    cfg = load_config(environ={"LORE_VAULT_API_ENRICH_WORKER": raw})
    assert cfg.api.enrich_worker is expected


def test_bool_setting_from_file_and_invalid_values(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[api]\nenrich_worker = false\n", encoding="utf-8")
    assert load_config(path, environ={}).api.enrich_worker is False
    path.write_text("[api]\nenrich_worker = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="布林"):
        load_config(path, environ={})
    with pytest.raises(ConfigError, match="布林"):
        load_config(environ={"LORE_VAULT_API_ENRICH_WORKER": "maybe"})


def test_query_timeout_must_be_positive():
    with pytest.raises(ConfigError, match="query_timeout"):
        load_config(environ={"LORE_VAULT_EMBEDDING_QUERY_TIMEOUT": "0"})


def test_api_token_only_from_env():
    from lore_vault.config import api_token

    assert api_token(environ={}) is None
    assert api_token(environ={"LORE_VAULT_API_TOKEN": "  "}) is None
    token = api_token(environ={"LORE_VAULT_API_TOKEN": SENTINEL})
    assert isinstance(token, Secret) and token.reveal() == SENTINEL
    assert SENTINEL not in repr(token)


def test_api_token_in_config_file_is_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'[api]\ntoken = "{SENTINEL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path, environ={})
    assert SENTINEL not in str(info.value)
