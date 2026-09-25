"""MCP 殼設定：沿用 `lore_vault.config` 的優先序（環境變數 > 設定檔 > 預設）與密鑰規則。

- 非密鑰項目在設定檔 `[mcp]` 區段，或環境變數 `LORE_VAULT_MCP_<項目>`
- bearer token 只從環境變數 `LORE_VAULT_API_TOKEN`（或 `--env-file`）讀
- Cloudflare Access service token：`CF_ACCESS_CLIENT_ID`／`CF_ACCESS_CLIENT_SECRET`
  來自環境變數、`--env-file`，或 `mcp.cf_access_env_file` 指向的檔案
- 密鑰一律包在 `Secret`，repr／錯誤訊息不含值
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from lore_vault.config import (
    API_TOKEN_ENV,
    ConfigError,
    Secret,
    api_token,
    cf_access_credentials,
    load_config,
)


@dataclass(frozen=True)
class ShellSettings:
    base_url: str
    token: Secret
    cf_access: tuple[Secret, Secret] | None = None
    timeout: float = 10.0
    snapshot_dir: Path | None = None
    snapshot_interval: float = 900.0
    snapshot_max_age_hours: float = 24.0
    # 殼啟動時是否在背景拉快照（測試關掉以便手動控制）
    snapshot_on_start: bool = True


def load_shell_settings(
    *,
    config_path: str | PathLike[str] | None = None,
    env_file: str | PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ShellSettings:
    config = load_config(config_path, env_file=env_file, environ=environ)
    mcp = config.mcp
    token = api_token(env_file=env_file, environ=environ)
    if token is None:
        raise ConfigError(f"未設定 {API_TOKEN_ENV}，MCP 殼無法向服務認證")
    cf_file = (
        Path(mcp.cf_access_env_file).expanduser() if mcp.cf_access_env_file else None
    )
    cf = cf_access_credentials(cf_env_file=cf_file, env_file=env_file, environ=environ)
    return ShellSettings(
        base_url=mcp.base_url.rstrip("/"),
        token=token,
        cf_access=cf,
        timeout=mcp.timeout,
        snapshot_dir=Path(mcp.snapshot_dir).expanduser() if mcp.snapshot_dir else None,
        snapshot_interval=mcp.snapshot_interval,
        snapshot_max_age_hours=mcp.snapshot_max_age_hours,
    )
