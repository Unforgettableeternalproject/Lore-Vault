"""schema 遷移（T-15）：`PRAGMA user_version` + 有序遷移函式。

- 每個遷移在自己的交易裡執行，`user_version` 在同一交易內更新；
  中途失敗整段 rollback，版本號不會停在半套 schema 上。
- 資料庫版本比程式新時拒絕開啟（舊程式不可在新 schema 上寫資料）。
- 不用 `executescript`：它會先隱式 COMMIT，破壞「一個遷移一個交易」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .errors import SchemaVersionError

# FTS5 tokenizer：`_` 算字元，snake_case 識別字保持完整（D1 實測）
FTS_TOKENIZE = "unicode61 tokenchars '_'"

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE vaults (
        key     TEXT PRIMARY KEY CHECK (length(trim(key)) > 0 AND key = lower(key)),
        display TEXT NOT NULL,
        kind    TEXT NOT NULL CHECK (kind IN ('repo', 'global')),
        created TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE vault_aliases (
        alias TEXT PRIMARY KEY CHECK (length(trim(alias)) > 0 AND alias = lower(alias)),
        vault TEXT NOT NULL REFERENCES vaults(key) ON DELETE CASCADE
    ) STRICT
    """,
    "CREATE INDEX vault_aliases_vault ON vault_aliases(vault)",
    # seq 是明確的 INTEGER PRIMARY KEY：VACUUM 不會重排，FTS／向量表用它當關聯鍵
    """
    CREATE TABLE notes (
        seq        INTEGER PRIMARY KEY,
        id         TEXT NOT NULL UNIQUE CHECK (length(trim(id)) > 0),
        vault      TEXT NOT NULL REFERENCES vaults(key),
        title      TEXT NOT NULL,
        summary    TEXT,
        body       TEXT NOT NULL,
        topics     TEXT NOT NULL DEFAULT '[]',
        links      TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT,
        created    TEXT NOT NULL,
        updated    TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX notes_vault_updated ON notes(vault, updated, id)",
    # 獨立內容的 FTS5 表，rowid = notes.seq；內容是 bigram 展開後的索引文字，
    # 由儲存層在同一交易內與 notes 同步（展開要用 Python，不能用 trigger）
    f"""
    CREATE VIRTUAL TABLE note_fts USING fts5(
        title, content, tokenize = "{FTS_TOKENIZE}"
    )
    """,
    """
    CREATE TABLE note_embeddings (
        note_seq INTEGER PRIMARY KEY REFERENCES notes(seq) ON DELETE CASCADE,
        dim      INTEGER NOT NULL CHECK (dim > 0),
        vector   BLOB NOT NULL,
        model    TEXT,
        updated  TEXT NOT NULL
    ) STRICT
    """,
    # episode／concept／injection 的 schema 型別沒有 vault 欄；
    # vault 由呼叫端在寫入當下解析後凍結在這裡（A7），完整記錄存在 data（JSON）。
    # JSON 省略 MISSING 欄位，三態（值／None／不存在）得以保留。
    """
    CREATE TABLE episodes (
        seq        INTEGER PRIMARY KEY,
        vault      TEXT NOT NULL REFERENCES vaults(key),
        session_id TEXT NOT NULL,
        prompt_id  TEXT NOT NULL,
        turn_index INTEGER NOT NULL,
        machine    TEXT NOT NULL,
        repo       TEXT,
        started_at TEXT,
        ended_at   TEXT,
        data       TEXT NOT NULL,
        recorded   TEXT NOT NULL,
        UNIQUE (session_id, prompt_id, turn_index)
    ) STRICT
    """,
    "CREATE INDEX episodes_vault_started ON episodes(vault, started_at)",
    """
    CREATE TABLE concepts (
        id          TEXT PRIMARY KEY CHECK (length(trim(id)) > 0),
        vault       TEXT NOT NULL REFERENCES vaults(key),
        kind        TEXT,
        scope_state TEXT NOT NULL CHECK (scope_state IN ('repo', 'global', 'missing')),
        scope       TEXT,
        data        TEXT NOT NULL,
        updated     TEXT NOT NULL,
        CHECK ((scope_state = 'repo') = (scope IS NOT NULL))
    ) STRICT
    """,
    "CREATE INDEX concepts_vault ON concepts(vault)",
    """
    CREATE TABLE injections (
        seq                INTEGER PRIMARY KEY,
        vault              TEXT NOT NULL REFERENCES vaults(key),
        session_id         TEXT NOT NULL,
        prompt_id          TEXT,
        prompt_fingerprint TEXT,
        data               TEXT NOT NULL,
        recorded           TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX injections_vault_session ON injections(vault, session_id)",
)


def _v1(conn: sqlite3.Connection) -> None:
    for statement in _V1_STATEMENTS:
        conn.execute(statement)


# 背景補算（summary／embedding）的嘗試紀錄與失敗狀態（T-19）。
# 佇列本身不存：「缺 summary／缺 embedding」直接從 notes 推導；這張表只記嘗試。
# for_updated：這些嘗試針對的 note 版本；note 內容變了，舊嘗試不再算數。
_V2_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE note_enrichment (
        note_seq     INTEGER NOT NULL REFERENCES notes(seq) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('summary', 'embedding')),
        for_updated  TEXT NOT NULL,
        attempts     INTEGER NOT NULL CHECK (attempts >= 0),
        status       TEXT NOT NULL CHECK (status IN ('pending', 'failed')),
        last_error   TEXT,
        last_attempt TEXT NOT NULL,
        next_attempt TEXT NOT NULL,
        PRIMARY KEY (note_seq, kind)
    ) STRICT
    """,
    "CREATE INDEX note_enrichment_status ON note_enrichment(kind, status)",
)


def _v2(conn: sqlite3.Connection) -> None:
    for statement in _V2_STATEMENTS:
        conn.execute(statement)


# 有序遷移：索引 i 的函式把版本從 i 升到 i+1。只能往後加，不可改動已發佈的項目。
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_v1, _v2)

SCHEMA_VERSION = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(
    conn: sqlite3.Connection,
    *,
    migrations: tuple[Callable[[sqlite3.Connection], None], ...] = MIGRATIONS,
) -> int:
    """把資料庫升到 `len(migrations)` 版；回傳升級後版本。

    連線必須是 autocommit 模式（`isolation_level=None`），交易由這裡明確控制。
    """
    target = len(migrations)
    version = current_version(conn)
    if version > target:
        raise SchemaVersionError(
            f"資料庫 schema 版本 {version} 比程式預期 {target} 新，拒絕開啟"
        )
    if conn.in_transaction:
        raise SchemaVersionError("遷移前連線不可有未結束的交易")
    while version < target:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 拿到寫鎖後重讀：另一個程序可能剛做完同一步
            version = current_version(conn)
            if version < target:
                migrations[version](conn)
                version += 1
                conn.execute(f"PRAGMA user_version = {version}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return current_version(conn)
