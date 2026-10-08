"""`python -m lore_vault.mcp [--config PATH] [--env-file PATH]`：啟動本地 stdio MCP 殼。

stdout 是 MCP 協定通道，log 一律走 stderr。設定錯誤時印出原因（不含密鑰）並以 2 結束。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from lore_vault.config import ConfigError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.mcp")
    parser.add_argument("--config", help="設定檔路徑（TOML，讀 [mcp] 區段）")
    parser.add_argument(
        "--env-file", help=".env 檔（LORE_VAULT_API_TOKEN、CF_ACCESS_* 等密鑰）"
    )
    args = parser.parse_args(argv)

    # Windows 主控台預設編碼不是 UTF-8；MCP client 以 UTF-8 讀 stderr
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx2 的 INFO 會逐筆記請求；只留警告
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    from . import task_plugin
    from .server import Shell, build_server
    from .settings import load_shell_settings

    try:
        settings = load_shell_settings(config_path=args.config, env_file=args.env_file)
    except ConfigError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2
    shell = Shell(settings)
    server = build_server(shell)
    task_plugin.register(server, shell)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
