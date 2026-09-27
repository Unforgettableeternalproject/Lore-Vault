"""手動整合測試（不進 pytest）：用真實 Ollama 與 OpenAI 對一則自造 note 跑一輪補算。

    uv run python scripts/enrich_smoke.py [--env-file .env] [--config config.toml]

- 資料庫建在系統暫存目錄，結束即刪；不碰任何正式資料。
- note 內容是自造的非敏感文字。
- 只輸出摘要文字、向量維度與統計；不輸出任何密鑰。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from lore_vault.config import load_config, openai_api_key
from lore_vault.enrich import EnrichWorker, OllamaEmbedder, OpenAISummarizer
from lore_vault.schema import Note, Vault
from lore_vault.storage import vectors
from lore_vault.storage.db import connect
from lore_vault.storage.notes import get_note, insert_note
from lore_vault.storage.timeutil import utc_now
from lore_vault.storage.vaults import upsert_vault

VAULT = "smoke/enrich"
TITLE = "儲存引擎選型：SQLite WAL 與 FTS5"
BODY = (
    "比較三個方案後決定採用 SQLite（WAL 模式）加 FTS5 全文索引，向量以 BLOB 存放、"
    "用 NumPy 暴力比對。以 1500 則 note、1024 維向量實測，單次向量查詢約 3 毫秒，"
    "BM25 查詢約 1 毫秒；中文採 bigram 切詞後召回率由 62% 提升到 91%。"
    "放棄 Postgres + pgvector，因為部署需要額外服務且這個資料量用不到 ANN 索引。"
)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(repo_root / ".env"))
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    env_file = args.env_file if Path(args.env_file).is_file() else None

    config = load_config(args.config, env_file=env_file)
    key = openai_api_key(env_file=env_file)
    if key is None:
        print("缺少 OPENAI_API_KEY", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "smoke.db")
        try:
            upsert_vault(conn, Vault(key=VAULT, display=VAULT, kind="repo"))
            now = utc_now()
            insert_note(
                conn,
                VAULT,
                Note(
                    id="smoke-1",
                    vault=VAULT,
                    title=TITLE,
                    body=BODY,
                    created=now,
                    updated=now,
                ),
            )
            worker = EnrichWorker(
                conn,
                config.worker,
                embedder=OllamaEmbedder(config.embedding),
                summarizer=OpenAISummarizer(config.summary, key),
            )
            stats = worker.run_once()
            note = get_note(conn, VAULT, "smoke-1")
            vector = vectors.get_embedding(conn, VAULT, "smoke-1")
            print(
                f"模型：summary={config.summary.model}（effort="
                f"{config.summary.reasoning_effort}），embedding={config.embedding.model}"
            )
            print(f"統計：{stats.to_dict()}")
            print(f"摘要：{note.summary}")
            print(f"摘要字數：{len(note.summary or '')}")
            print(f"向量維度：{None if vector is None else vector.shape[0]}")
            ok = note.summary is not None and vector is not None
        finally:
            conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
