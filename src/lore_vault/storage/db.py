"""連線與交易。資料庫路徑一律由呼叫端傳入；import 本模組不建檔、不讀設定。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path

from .errors import StorageError
from .migrate import migrate

BUSY_TIMEOUT_MS = 5000


def connect(
    path: str | PathLike[str], *, run_migrations: bool = True
) -> sqlite3.Connection:
    """開啟資料庫：WAL、外鍵、autocommit（交易由 `transaction` 明確控制）。

    `journal_mode` 未真的切成 WAL（例如 `:memory:`、唯讀媒體）時直接拋錯，
    不在非 WAL 模式下默默運作。
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StorageError(f"無法啟用 WAL，journal_mode={mode!r}（路徑：{path}）")
        # 外鍵是每條連線各自的設定，每次開啟都要重設
        conn.execute("PRAGMA foreign_keys = ON")
        if run_migrations:
            migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: str | PathLike[str]) -> sqlite3.Connection:
    """唯讀開啟（doctor 對帳用）：不建檔、不遷移——遷移會蓋掉「版本不符」這個事實。"""
    target = Path(path)
    if not target.is_file():
        raise StorageError(f"資料庫檔案不存在：{target}")
    conn = sqlite3.connect(
        f"{target.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """寫入交易（BEGIN IMMEDIATE）。已在交易中時直接沿用外層，由外層決定提交。

    主表與 FTS／向量表的同步寫入都包在同一個交易內。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
