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


# 外部來源匯入的對帳清單（T-37）：匯入前先整批寫入「來源應有什麼」，再逐筆匯入 note。
# 刻意不設指向 notes 的外鍵：note 遺失或被刪時清單仍在，doctor 才看得出漏筆。
# - import_sources：每則來源 note 一列。content_sha256 = 來源 (title, body) 雜湊；
#   imported_updated 為 NULL 表示清單已登記、note 尚未成功匯入。
# - import_vault_counts：每個 vault 的來源筆數（對照來源端自己回報的數字）。
_V3_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE import_sources (
        source           TEXT NOT NULL CHECK (length(trim(source)) > 0),
        source_id        TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
        note_id          TEXT NOT NULL CHECK (length(trim(note_id)) > 0),
        vault            TEXT NOT NULL,
        content_sha256   TEXT NOT NULL CHECK (length(content_sha256) = 64),
        source_updated   TEXT NOT NULL,
        imported_updated TEXT,
        imported_at      TEXT,
        PRIMARY KEY (source, source_id)
    ) STRICT
    """,
    "CREATE UNIQUE INDEX import_sources_note ON import_sources(note_id)",
    "CREATE INDEX import_sources_vault ON import_sources(source, vault)",
    """
    CREATE TABLE import_vault_counts (
        source       TEXT NOT NULL,
        vault        TEXT NOT NULL,
        source_count INTEGER NOT NULL CHECK (source_count >= 0),
        recorded     TEXT NOT NULL,
        PRIMARY KEY (source, vault)
    ) STRICT
    """,
)


def _v3(conn: sqlite3.Connection) -> None:
    for statement in _V3_STATEMENTS:
        conn.execute(statement)


# spike 接入（階段 8）：
# - vaults.origin：vault 從哪條路徑建立。'manual' = 明確建立（POST /v1/vaults、匯入、
#   upsert_vault），'episode' = episode 收料時自動建，'pipeline' = 管線寫通用 concept 時
#   自動建 `global`。origin_detail 是自動建立當下的觸發來源（JSON），供日後人工審視。
#   既有列一律視為 'manual'。
# - concepts.ord：匯出順序。spike 的 concepts.json 是有序清單，注入 scorer 以穩定排序
#   取前 K 筆，同分時靠清單順序決勝——順序是行為的一部分，不可用 id 排序頂替。
#   既有列以 rowid（寫入順序）回填。
# - episodes(machine, recorded)：doctor 依機器查最近收料時間。
_V4_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE vaults ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE vaults ADD COLUMN origin_detail TEXT",
    "ALTER TABLE concepts ADD COLUMN ord INTEGER NOT NULL DEFAULT 0",
    "UPDATE concepts SET ord = rowid",
    "CREATE INDEX concepts_ord ON concepts(ord)",
    "CREATE INDEX episodes_machine_recorded ON episodes(machine, recorded)",
)


def _v4(conn: sqlite3.Connection) -> None:
    for statement in _V4_STATEMENTS:
        conn.execute(statement)


# 刪除墓碑：管理指令刪掉的 note 記在這裡，重跑外部匯入時跳過，不讓它匯回來；
# 匯入對帳把有墓碑的清單列算成「刻意刪除」而不是漏匯。
# 刻意不設指向 notes／vaults 的外鍵：note 與 vault 都已刪除，墓碑要比它們活得久。
# source／source_id：刪除當下對帳清單記載的來源（非匯入的 note 為 NULL）。
_V5_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE note_tombstones (
        note_id    TEXT PRIMARY KEY CHECK (length(trim(note_id)) > 0),
        vault      TEXT NOT NULL,
        source     TEXT,
        source_id  TEXT,
        deleted_at TEXT NOT NULL,
        reason     TEXT NOT NULL,
        CHECK ((source IS NULL) = (source_id IS NULL))
    ) STRICT
    """,
    "CREATE INDEX note_tombstones_source ON note_tombstones(source, source_id)",
    "CREATE INDEX note_tombstones_vault ON note_tombstones(vault)",
)


def _v5(conn: sqlite3.Connection) -> None:
    for statement in _V5_STATEMENTS:
        conn.execute(statement)


# concept 寫回依 source_turns 查來源 episode 的 vault（A17，
# `api.spike._prefetch_turn_vaults` 以 `prompt_id IN (...)` 分塊查）。唯一鍵
# (session_id, prompt_id, turn_index) 以 session_id 開頭用不上，沒有這個索引
# 每一塊都掃全表。
_V6_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX episodes_prompt_turn ON episodes(prompt_id, turn_index)",
)


def _v6(conn: sqlite3.Connection) -> None:
    for statement in _V6_STATEMENTS:
        conn.execute(statement)


# space 分群（A18，T-52）：vault 所屬的 space。既有 vault 全歸 dev（預設值即回填）。
# 不加 CHECK：ALTER TABLE ADD COLUMN 無法對既有資料回填檢查；合法值由
# `schema.SPACES` 白名單與 doctor `space.valid_values` 把關。
_V7_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE vaults ADD COLUMN space TEXT NOT NULL DEFAULT 'dev'",
    "CREATE INDEX vaults_space ON vaults(space)",
)


def _v7(conn: sqlite3.Connection) -> None:
    for statement in _V7_STATEMENTS:
        conn.execute(statement)


# 有序遷移：索引 i 的函式把版本從 i 升到 i+1。只能往後加，不可改動已發佈的項目。
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _v1,
    _v2,
    _v3,
    _v4,
    _v5,
    _v6,
    _v7,
)

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
