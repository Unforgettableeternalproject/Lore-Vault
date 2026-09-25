"""執行環境的 SQLite 能力斷言（純標準庫）：版本 ≥ 3.37（STRICT 表）且有 FTS5。

容器建置與啟動時都跑 `python -m lore_vault.storage.sqlite_check`；不符就印出
明確原因並以 exit code 1 結束，不讓服務在缺能力的 SQLite 上啟動後才在遷移時爆。
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence

from .migrate import FTS_TOKENIZE

# STRICT 表需要 3.37.0
MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 37, 0)


def sqlite_problems(
    *, version_info: Sequence[int] | None = None, connect=sqlite3.connect
) -> list[str]:
    """回傳不符合的項目；空清單代表通過。參數供測試注入。"""
    info = tuple(
        version_info if version_info is not None else sqlite3.sqlite_version_info
    )
    problems: list[str] = []
    if info < MIN_SQLITE_VERSION:
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        have = ".".join(map(str, info))
        problems.append(f"SQLite 版本 {have} 過舊，需要 ≥ {need}（STRICT 表）")
    conn = connect(":memory:")
    try:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE fts_probe USING fts5("
                f'x, tokenize = "{FTS_TOKENIZE}")'
            )
        except sqlite3.Error as exc:
            problems.append(f"SQLite 缺 FTS5 或 tokenizer 不可用：{exc}")
        try:
            conn.execute("CREATE TABLE strict_probe (x INTEGER) STRICT")
        except sqlite3.Error as exc:
            problems.append(f"SQLite 不支援 STRICT 表：{exc}")
    finally:
        conn.close()
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    problems = sqlite_problems()
    if problems:
        for problem in problems:
            print(f"[sqlite_check] 失敗：{problem}", file=sys.stderr)
        return 1
    print(f"[sqlite_check] SQLite {sqlite3.sqlite_version}：STRICT 與 FTS5 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
