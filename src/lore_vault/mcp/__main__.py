"""`python -m lore_vault.mcp [--config PATH] [--env-file PATH]`：啟動本地 stdio MCP 殼。

stdout 是 MCP 協定通道，log 一律走 stderr。設定錯誤時印出原因（不含密鑰）並以 2 結束。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from lore_vault.config import ConfigError

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_HANDLER_TAG = "lore_vault_mcp_shell_file"


def attach_file_log(path: Path) -> bool:
    """掛上輪替 log 檔（1MB × 3 份備份）到 root logger；已掛過先換掉，不重複寫。

    stdout 是 MCP 通道、stderr 又常被客戶端丟棄，traceback 要落檔才查得到。
    檔案不能開（權限、唯讀磁碟）時只警告，殼照常運作；回傳是否成功。
    """
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _FILE_HANDLER_TAG, False)]:
        root.removeHandler(old)
        old.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("無法開啟 log 檔 %s：%s", path, exc)
        return False
    handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(handler, _FILE_HANDLER_TAG, True)
    root.addHandler(handler)
    return True


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
        format=_FORMAT,
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
    attach_file_log(settings.log_path)
    shell = Shell(settings)
    server = build_server(shell)
    task_plugin.register(server, shell)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
