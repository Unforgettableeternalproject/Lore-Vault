"""背景補算的命令列進入點。

    python -m lore_vault.enrich --once [--limit N] [--config PATH] [--env-file PATH]
                                [--db PATH]
    python -m lore_vault.enrich              # 常駐：每 worker.poll_interval 秒一輪
    python -m lore_vault.enrich --reset-failed [summary|embedding|all]

資料庫路徑：`--db` > 設定 `database.path`（含環境變數 LORE_VAULT_DATABASE_PATH）。
輸出只含統計與錯誤摘要，不含任何密鑰。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from lore_vault.config import ConfigError, load_config, openai_api_key
from lore_vault.storage import enrichment as store
from lore_vault.storage.db import connect

from .clients import OllamaEmbedder, OpenAISummarizer, Transport, urllib_transport
from .worker import EnrichWorker, RateLimiter


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport = urllib_transport,
    stdout: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.enrich")
    parser.add_argument("--once", action="store_true", help="只跑一輪就結束")
    parser.add_argument("--limit", type=int, default=None, help="本輪每種最多幾則")
    parser.add_argument("--config", help="設定檔路徑（TOML）")
    parser.add_argument("--env-file", help=".env 路徑（讀 OPENAI_API_KEY 等）")
    parser.add_argument("--db", help="資料庫路徑（覆寫設定）")
    parser.add_argument(
        "--reset-failed",
        choices=["summary", "embedding", "all"],
        help="清掉失敗紀錄讓下一輪重試",
    )
    args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必須大於 0")

    try:
        config = load_config(args.config, env_file=args.env_file)
        api_key = openai_api_key(env_file=args.env_file)
    except ConfigError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 2
    db_path = args.db or config.database.path
    if not db_path:
        print(
            "缺少資料庫路徑：用 --db 或設定 database.path"
            "（環境變數 LORE_VAULT_DATABASE_PATH）",
            file=sys.stderr,
        )
        return 2

    conn = connect(Path(db_path))
    try:
        if args.reset_failed:
            kind = None if args.reset_failed == "all" else args.reset_failed
            count = store.reset_failed(conn, kind)
            print(json.dumps({"reset": count}, ensure_ascii=False), file=out)
            return 0
        unavailable: dict[str, str] = {}
        summarizer = None
        if api_key is None:
            unavailable["summary"] = "缺少 OPENAI_API_KEY，略過摘要"
        else:
            summarizer = OpenAISummarizer(config.summary, api_key, transport=transport)
        worker = EnrichWorker(
            conn,
            config.worker,
            embedder=OllamaEmbedder(config.embedding, transport=transport),
            summarizer=summarizer,
            embed_limiter=RateLimiter(config.embedding.rate_per_minute, sleep=sleep),
            summary_limiter=RateLimiter(config.summary.rate_per_minute, sleep=sleep),
            unavailable=unavailable,
        )
        while True:
            stats = worker.run_once(limit=args.limit)
            print(json.dumps(stats.to_dict(), ensure_ascii=False), file=out)
            out.flush()
            if args.once:
                return 0
            sleep(config.worker.poll_interval)
    finally:
        conn.close()
