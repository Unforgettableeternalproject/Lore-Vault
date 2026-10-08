"""schema 遷移（T-15）：`PRAGMA user_version` + 有序遷移函式。

- 每個遷移在自己的交易裡執行，`user_version` 在同一交易內更新；
  中途失敗整段 rollback，版本號不會停在半套 schema 上。
- 資料庫版本比程式新時拒絕開啟（舊程式不可在新 schema 上寫資料）。
- 不用 `executescript`：它會先隱式 COMMIT，破壞「一個遷移一個交易」。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from .errors import SchemaVersionError
from .timeutil import utc_now

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


# v8 發佈時的抽取錯誤碼（A19、設計 4.3），展開進 v8 的 CHECK。
# 已發佈、不可改：改了會讓新建庫與既有庫的 v8 不一致。要新增錯誤碼時另加一個
# 遷移重建 CHECK，並把 DOCUMENT_ERROR_CODES 指向新的清單。
_V8_ERROR_CODES = (
    "encrypted",
    "corrupt",
    "empty_extraction",
    "too_large",
    "unsupported_format",
    "unsupported_encoding",
)
_V8_ERROR_CODE_LIST = ", ".join(f"'{code}'" for code in _V8_ERROR_CODES)

# 目前 schema 接受的錯誤碼；與 `documents.extract.ERROR_CODES` 同步（測試比對）
DOCUMENT_ERROR_CODES = _V8_ERROR_CODES

# 文件存儲（A19，T-58）：
# - documents：每個 vault 各自一列 metadata；space 由 vault 決定，查詢一律經
#   `vaults.vault_clause`。原始檔以 sha256 內容定址存在 blob 目錄（跨 vault 共用）。
#   (vault, sha256) 刻意不設 UNIQUE：同 vault 去重由上傳端處理（T-67），
#   failed 後重新上傳的語意尚未定案，唯一鍵會先把路堵死。
#   status='failed' 與 error_code 非空互為充要條件；錯誤碼只收白名單。
# - document_chunks：seq 是 FTS／向量表的關聯鍵（比照 notes.seq）；locator 為 JSON。
# - chunk_fts：比照 note_fts（有內容、rowid = document_chunks.seq、CJK bigram 由
#   Python 展開）。設計草案寫 contentless（content=''），但 contentless 表刪列需要
#   contentless_delete=1（SQLite 3.43+，高於 sqlite_check 的 3.37 下限），而版本
#   取代與刪除都要能清索引列，所以改用與 note_fts 同一套。
# - document_chunk_embeddings：比照 note_embeddings，隨 chunk CASCADE 刪除。
# - document_tombstones：刻意無外鍵（document 與 vault 都刪了，墓碑還要在）。
# - document_enrichment：抽取的嘗試／退避紀錄（佇列本身由 status='pending' 推導）。
#   文件內容不可變（新版本＝新列），不需要 note_enrichment 的 for_updated。
_V8_STATEMENTS: tuple[str, ...] = (
    f"""
    CREATE TABLE documents (
        id           TEXT PRIMARY KEY CHECK (id LIKE 'doc:_%'),
        vault        TEXT NOT NULL REFERENCES vaults(key),
        filename     TEXT NOT NULL CHECK (length(trim(filename)) > 0),
        mime         TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256       TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 = lower(sha256)
            AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        version      INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        supersedes   TEXT REFERENCES documents(id),
        status       TEXT NOT NULL
            CHECK (status IN ('pending', 'extracting', 'ready', 'failed')),
        error_code   TEXT
            CHECK (error_code IS NULL OR error_code IN ({_V8_ERROR_CODE_LIST})),
        error_detail TEXT,
        chunk_count  INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
        created      TEXT NOT NULL,
        updated      TEXT NOT NULL,
        CHECK ((status = 'failed') = (error_code IS NOT NULL)),
        CHECK (supersedes IS NULL OR supersedes != id)
    ) STRICT
    """,
    "CREATE INDEX documents_vault_updated ON documents(vault, updated, id)",
    "CREATE INDEX documents_vault_sha256 ON documents(vault, sha256)",
    "CREATE INDEX documents_sha256 ON documents(sha256)",
    "CREATE INDEX documents_status ON documents(status, updated)",
    "CREATE INDEX documents_supersedes ON documents(supersedes)",
    """
    CREATE TABLE document_chunks (
        seq         INTEGER PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(id),
        idx         INTEGER NOT NULL CHECK (idx >= 0),
        text        TEXT NOT NULL,
        locator     TEXT NOT NULL CHECK (json_valid(locator)),
        UNIQUE (document_id, idx)
    ) STRICT
    """,
    f"""
    CREATE VIRTUAL TABLE chunk_fts USING fts5(
        content, tokenize = "{FTS_TOKENIZE}"
    )
    """,
    """
    CREATE TABLE document_chunk_embeddings (
        chunk_seq INTEGER PRIMARY KEY
            REFERENCES document_chunks(seq) ON DELETE CASCADE,
        dim       INTEGER NOT NULL CHECK (dim > 0),
        vector    BLOB NOT NULL,
        model     TEXT,
        updated   TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE document_tombstones (
        document_id TEXT PRIMARY KEY CHECK (length(trim(document_id)) > 0),
        vault       TEXT NOT NULL,
        sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
        deleted_at  TEXT NOT NULL,
        reason      TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX document_tombstones_vault ON document_tombstones(vault)",
    """
    CREATE TABLE document_enrichment (
        document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('extract')),
        attempts     INTEGER NOT NULL CHECK (attempts >= 0),
        status       TEXT NOT NULL CHECK (status IN ('pending', 'failed')),
        last_error   TEXT,
        last_attempt TEXT NOT NULL,
        next_attempt TEXT NOT NULL,
        PRIMARY KEY (document_id, kind)
    ) STRICT
    """,
    "CREATE INDEX document_enrichment_status ON document_enrichment(kind, status)",
)


def _v8(conn: sqlite3.Connection) -> None:
    for statement in _V8_STATEMENTS:
        conn.execute(statement)


# 文件抽取 worker 與索引（T-63～T-67）：
# - documents.encoding：文字類文件偵測到的編碼（utf-8／utf-8-sig／utf-16／cp950）；
#   二進位格式與尚未抽取為 NULL
# - document_chunks.overlap：本 chunk 開頭與前一個 chunk 重疊的字元數。
#   `get(doc:…)` 串接全文時略過重疊段，才能還原原文而不重複
# - document_enrichment.kind 加 'embedding'：chunk 向量補算的嘗試／失敗以文件為單位
#   記錄（有上限重試）。STRICT 表無法改 CHECK，重建（沒有其他表參照它）
_V9_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE documents ADD COLUMN encoding TEXT",
    """
    ALTER TABLE document_chunks
        ADD COLUMN overlap INTEGER NOT NULL DEFAULT 0 CHECK (overlap >= 0)
    """,
    """
    CREATE TABLE document_enrichment_v9 (
        document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('extract', 'embedding')),
        attempts     INTEGER NOT NULL CHECK (attempts >= 0),
        status       TEXT NOT NULL CHECK (status IN ('pending', 'failed')),
        last_error   TEXT,
        last_attempt TEXT NOT NULL,
        next_attempt TEXT NOT NULL,
        PRIMARY KEY (document_id, kind)
    ) STRICT
    """,
    """
    INSERT INTO document_enrichment_v9
    SELECT document_id, kind, attempts, status, last_error, last_attempt, next_attempt
    FROM document_enrichment
    """,
    "DROP TABLE document_enrichment",
    "ALTER TABLE document_enrichment_v9 RENAME TO document_enrichment",
    "CREATE INDEX document_enrichment_status ON document_enrichment(kind, status)",
)


def _v9(conn: sqlite3.Connection) -> None:
    for statement in _V9_STATEMENTS:
        conn.execute(statement)


# 抽取品質警示（v10）：documents.warnings 為 JSON 陣列 `[{"code", "detail"}, ...]`，
# 沒有警示為 NULL。目前只有 cp950 判定信心低（encoding_low_confidence）；
# 成功抽取但結果可能不可靠，doctor `documents.quality_warnings` 以 warn 列出
_V10_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE documents ADD COLUMN warnings TEXT
        CHECK (warnings IS NULL OR json_valid(warnings))
    """,
)


def _v10(conn: sqlite3.Connection) -> None:
    for statement in _V10_STATEMENTS:
        conn.execute(statement)


# 文件復原與人工重試（v11，T-74）：
# - document_tombstones 補上重建 documents 列所需的 metadata（filename／mime／
#   size_bytes／version）。舊墓碑為 NULL，復原時明確拒絕、不猜
# - documents.manual_retries：經管理端點人工重排抽取的次數（有上限）。
#   `reset_for_retry` 會清掉 document_enrichment 的嘗試紀錄，人工次數不能放那裡
_V11_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE document_tombstones ADD COLUMN filename TEXT",
    "ALTER TABLE document_tombstones ADD COLUMN mime TEXT",
    "ALTER TABLE document_tombstones ADD COLUMN size_bytes INTEGER",
    "ALTER TABLE document_tombstones ADD COLUMN version INTEGER",
    """
    ALTER TABLE documents
        ADD COLUMN manual_retries INTEGER NOT NULL DEFAULT 0
        CHECK (manual_retries >= 0)
    """,
)


def _v11(conn: sqlite3.Connection) -> None:
    for statement in _V11_STATEMENTS:
        conn.execute(statement)


# 作者契約與可復原的 note 刪除（v12，A22）：
# - notes.author：寫入者自報的名稱（未填 NULL，不代填）；principal：服務依憑證判定
#   的主體；updated_by／updated_by_principal：最後一次寫入者。可為 NULL 且不設
#   DEFAULT：漏設 principal 的寫入路徑不會被默默記成某人，由儲存層拒收、doctor
#   `notes.attribution` 對帳
# - 回填：principal 一律 'xavier'（v12 前唯一的憑證）；舊 PM 匯入成功的 note（對帳
#   清單 imported_updated 非 NULL）author／updated_by 標 'legacy'，其餘 author 維持
#   NULL。清單有列但 imported_updated 為 NULL 的是「id 已存在但非本工具匯入」，不算
# - note_tombstones.snapshot：刪除當下 note 的完整內容（JSON），undelete 據此還原原 id；
#   v12 前的舊墓碑為 NULL，維持「只移除墓碑」的舊行為
_V12_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE notes ADD COLUMN author TEXT",
    "ALTER TABLE notes ADD COLUMN principal TEXT",
    "ALTER TABLE notes ADD COLUMN updated_by TEXT",
    "ALTER TABLE notes ADD COLUMN updated_by_principal TEXT",
    "UPDATE notes SET principal = 'xavier', updated_by_principal = 'xavier'",
    """
    UPDATE notes SET author = 'legacy', updated_by = 'legacy'
    WHERE id IN (
        SELECT note_id FROM import_sources WHERE imported_updated IS NOT NULL
    )
    """,
    """
    ALTER TABLE note_tombstones ADD COLUMN snapshot TEXT
        CHECK (snapshot IS NULL OR json_valid(snapshot))
    """,
)


def _v12(conn: sqlite3.Connection) -> None:
    for statement in _V12_STATEMENTS:
        conn.execute(statement)


# UI 帳號密碼登入與全域鎖定（v13，A23；規則見 `storage.ui_login`）：
# - ui_accounts：username 即 session 的 principal；儲存保留大小寫、比對不分大小寫
#   （COLLATE NOCASE，也讓大小寫不同的同名帳號無法並存）。密碼只存 scrypt 雜湊，
#   salt 與參數（n／r／p／dklen）寫在列中，日後調整參數不影響舊雜湊的驗證
# - ui_login_state：單列（id = 1）的全域失敗計數與鎖定狀態；服務重啟不歸零、不解鎖
# - ui_login_log：每次嘗試一列（不含密碼）；result 另含人工解鎖的 'unlock'
# - principal 改名：唯一的主體由 'xavier' 改為 'UEPBernie'（與 Eternity 帳號一致）。
#   notes 的 principal／updated_by_principal 與 note 墓碑快照 JSON 內的同名欄位一併
#   改寫；不動 notes.updated（樂觀鎖版本）
_V13_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE ui_accounts (
        username      TEXT PRIMARY KEY COLLATE NOCASE
                      CHECK (length(username) BETWEEN 1 AND 64
                             AND username = trim(username)),
        display       TEXT NOT NULL CHECK (length(trim(display)) > 0),
        password_hash BLOB NOT NULL,
        salt          BLOB NOT NULL,
        scrypt_n      INTEGER NOT NULL CHECK (scrypt_n > 1),
        scrypt_r      INTEGER NOT NULL CHECK (scrypt_r > 0),
        scrypt_p      INTEGER NOT NULL CHECK (scrypt_p > 0),
        dklen         INTEGER NOT NULL CHECK (dklen >= 16),
        created       TEXT NOT NULL,
        updated       TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE ui_login_state (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        failures    INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
        failure_day TEXT,
        locked_at   TEXT,
        updated     TEXT
    ) STRICT
    """,
    "INSERT INTO ui_login_state (id, failures) VALUES (1, 0)",
    """
    CREATE TABLE ui_login_log (
        seq      INTEGER PRIMARY KEY,
        at       TEXT NOT NULL,
        ip       TEXT NOT NULL,
        username TEXT,
        result   TEXT NOT NULL CHECK (result IN
                 ('success', 'bad_credentials', 'locked', 'no_account', 'unlock'))
    ) STRICT
    """,
    "CREATE INDEX ui_login_log_at ON ui_login_log(at)",
    "UPDATE notes SET principal = 'UEPBernie' WHERE principal = 'xavier'",
    """
    UPDATE notes SET updated_by_principal = 'UEPBernie'
    WHERE updated_by_principal = 'xavier'
    """,
    """
    UPDATE note_tombstones
    SET snapshot = json_set(snapshot, '$.principal', 'UEPBernie')
    WHERE snapshot IS NOT NULL AND json_extract(snapshot, '$.principal') = 'xavier'
    """,
    """
    UPDATE note_tombstones
    SET snapshot = json_set(snapshot, '$.updated_by_principal', 'UEPBernie')
    WHERE snapshot IS NOT NULL
      AND json_extract(snapshot, '$.updated_by_principal') = 'xavier'
    """,
)


def _v13(conn: sqlite3.Connection) -> None:
    for statement in _V13_STATEMENTS:
        conn.execute(statement)


# 補算入列時間（v14）：`notes.enqueued` = 服務寫入目前版本的牆鐘時間（新增、更新、
# 取消刪除都重設）。補算佇列是推導的（缺 summary／embedding），入列時間原本借用
# `updated`；舊 PM 匯入與還原的 note 保留原始 `updated`，doctor `enrich.backlog`
# 因此算出極大的等待時間。STRICT 表 ADD COLUMN 不能給非常數預設，只能 nullable，
# 由寫入端一律填值、doctor `enrich.queue_time` 對帳 NULL。
# 回填：遷移前的真實入列時間無從得知——
# - 目前待補算（缺 summary 或缺 embedding，且不是「目前版本已標記失敗」）→ 遷移當下。
#   取捨：原生 note 若真的卡了很久，升級當下等待時間歸零一次，worker 仍沒跑時
#   要再過 backlog 門檻（預設 1 小時）才會 warn；
#   換來升級後不會因舊匯入 note 瞬間大量 warn
# - 其餘（已補算完成或已標記失敗，不在佇列內）→ 沿用 `updated`
def _v14(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE notes ADD COLUMN enqueued TEXT")
    conn.execute("UPDATE notes SET enqueued = updated")
    conn.execute(
        """
        UPDATE notes SET enqueued = ?
        WHERE seq IN (
            SELECT n.seq FROM notes n
            WHERE (n.summary IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM note_enrichment e
                       WHERE e.note_seq = n.seq AND e.kind = 'summary'
                         AND e.status = 'failed' AND e.for_updated = n.updated))
               OR (NOT EXISTS (
                       SELECT 1 FROM note_embeddings v WHERE v.note_seq = n.seq)
                   AND NOT EXISTS (
                       SELECT 1 FROM note_enrichment e
                       WHERE e.note_seq = n.seq AND e.kind = 'embedding'
                         AND e.status = 'failed' AND e.for_updated = n.updated))
        )
        """,
        (utc_now(),),
    )


# 執行期設定（v15，D13；白名單與驗證見 `lore_vault.runtime_settings`）：
# - settings_overrides：UI 設定頁存的覆寫值（JSON），覆寫設定檔／環境變數的預設值。
#   刻意不設 CHECK 限定鍵名：白名單在程式裡，doctor `settings.overrides` 對帳
# - settings_audit：每次修改或還原一列（誰、何時、生效值舊→新）。
#   與覆寫列在同一交易寫入，
#   doctor `settings.audit_agreement` 核對「目前覆寫值 = 該鍵最後一筆稽核的新值」
_V15_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE settings_overrides (
        key        TEXT PRIMARY KEY CHECK (length(trim(key)) > 0),
        value      TEXT NOT NULL CHECK (json_valid(value)),
        updated    TEXT NOT NULL,
        updated_by TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE settings_audit (
        seq       INTEGER PRIMARY KEY,
        at        TEXT NOT NULL,
        key       TEXT NOT NULL,
        action    TEXT NOT NULL CHECK (action IN ('set', 'reset')),
        old_value TEXT NOT NULL CHECK (json_valid(old_value)),
        new_value TEXT NOT NULL CHECK (json_valid(new_value)),
        principal TEXT NOT NULL,
        display   TEXT
    ) STRICT
    """,
    "CREATE INDEX settings_audit_key ON settings_audit(key, seq)",
)


def _v15(conn: sqlite3.Connection) -> None:
    for statement in _V15_STATEMENTS:
        conn.execute(statement)


# 雜項 vault（v16，D14；路由見 `storage.vaults.route_episode_vault`／
# `route_injection_vault`）：
# - vaults.kind 加 'misc'。vaults 被多張表以外鍵參照，照 12 步重建要在交易外關外鍵、
#   改動遷移框架；這裡只「放寬」CHECK（不改磁碟格式、既有資料必然仍合法），採 SQLite
#   ALTER TABLE 文件第 7 節的 writable_schema 程序，在同一個遷移交易內完成，
#   失敗整段 rollback。原字串必須恰好出現一次，否則大聲失敗、不靜默略過
# - episodes.origin_key／injections.origin_key：被改路由到雜項 vault 的列，記下收料時
#   客戶端送來的原始 binding key（canonical，`folder/<名稱>`）；其餘為 NULL。
#   不動 data 與其他凍結欄位。不加 CHECK，由 doctor `vaults.misc_routing` 對帳
# - 既有資料：episode 收料自動建立的 `folder/*` vault（origin='episode'）是新規則下
#   本該進雜項的位置（D14「未註冊位置一律歸雜項」）。note／文件／墓碑／別名任一
#   非零 → 整個不動（遷移不能互動停下，交給 doctor 以 warn 呈現）；否則 episode、
#   injection（記 origin_key）與由它們衍生的 concept 一併改歸雜項，vault 清空後刪除，
#   搬移紀錄寫進雜項 vault 的 origin_detail（JSON，供人工審視）。concept 沒有
#   origin_key：來源位置可由 source_turns 對回 episode 的 origin_key 追溯
_V16_KIND_CHECK_OLD = "CHECK (kind IN ('repo', 'global'))"
_V16_KIND_CHECK_NEW = "CHECK (kind IN ('repo', 'global', 'misc'))"
_V16_MISC_KEY = "misc"
_V16_MISC_DISPLAY = "雜項"


def _v16_relax_vault_kind(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'vaults'"
    ).fetchone()
    sql = row[0] if row is not None else ""
    if sql.count(_V16_KIND_CHECK_OLD) != 1:
        raise SchemaVersionError(
            "v16：vaults 的 kind CHECK 與預期不符，拒絕改寫 schema"
        )
    version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute(
            "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = 'vaults'",
            (sql.replace(_V16_KIND_CHECK_OLD, _V16_KIND_CHECK_NEW),),
        )
        conn.execute(f"PRAGMA schema_version = {version + 1}")
    finally:
        conn.execute("PRAGMA writable_schema = OFF")


def _v16_count(conn: sqlite3.Connection, table: str, column: str, key: str) -> int:
    return int(
        conn.execute(
            f"SELECT count(*) FROM {table} WHERE {column} = ?", (key,)
        ).fetchone()[0]
    )


def absorb_folder_vaults(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """把 episode 收料自動建立的 `folder/*` vault 併入雜項 vault（v16 的資料部分）。

    冪等：併完的 vault 已刪除，不再是候選；被跳過的每次都同樣跳過、不寫紀錄。
    回傳每個候選的處理紀錄 `{key, action, 各表筆數}`；action：
    - `skipped_has_content`：有 note／文件／墓碑／別名，完全不動
    - `skipped_misc_conflict`：key `misc` 已被非雜項 vault 佔用，完全不動
    - `moved_and_removed`：episode／injection／concept 已搬進雜項、vault 已刪除
    呼叫端須已在交易內。
    """
    candidates = [
        r[0]
        for r in conn.execute(
            """
            SELECT key FROM vaults
            WHERE space = 'dev' AND origin = 'episode' AND key LIKE 'folder/%'
              AND kind = 'repo'
            ORDER BY key
            """
        )
    ]
    records: list[dict[str, object]] = []
    for key in candidates:
        moving = {
            "episodes": _v16_count(conn, "episodes", "vault", key),
            "injections": _v16_count(conn, "injections", "vault", key),
            "concepts": _v16_count(conn, "concepts", "vault", key),
        }
        blocking = {
            "notes": _v16_count(conn, "notes", "vault", key),
            "documents": _v16_count(conn, "documents", "vault", key),
            "note_tombstones": _v16_count(conn, "note_tombstones", "vault", key),
            "document_tombstones": _v16_count(
                conn, "document_tombstones", "vault", key
            ),
            # 雜項 vault 依保留規則不能收別名，有別名的 vault 交人工處理
            "aliases": _v16_count(conn, "vault_aliases", "vault", key),
        }
        if any(blocking.values()):
            records.append(
                {"key": key, "action": "skipped_has_content", **moving, **blocking}
            )
            continue
        misc = conn.execute(
            "SELECT kind FROM vaults WHERE key = ?", (_V16_MISC_KEY,)
        ).fetchone()
        if misc is not None and misc[0] != "misc":
            records.append({"key": key, "action": "skipped_misc_conflict", **moving})
            continue
        if misc is None:
            conn.execute(
                """
                INSERT INTO vaults (key, display, kind, created, origin, origin_detail,
                                    space)
                VALUES (?, ?, 'misc', ?, 'episode', ?, 'dev')
                """,
                (
                    _V16_MISC_KEY,
                    _V16_MISC_DISPLAY,
                    utc_now(),
                    json.dumps(
                        {
                            "reason": "schema v16 併入 folder/* vault",
                            "created_at": utc_now(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        for table in ("episodes", "injections"):
            conn.execute(
                f"UPDATE {table} SET vault = ?, origin_key = ? WHERE vault = ?",
                (_V16_MISC_KEY, key, key),
            )
        # concept 是那批 episode 的管線衍生物（A17 依 source_turns 歸屬）：今天同樣的
        # episode 會落在雜項，concept 跟著搬才一致；ord（匯出順序）不動
        conn.execute(
            "UPDATE concepts SET vault = ? WHERE vault = ?", (_V16_MISC_KEY, key)
        )
        conn.execute("DELETE FROM vaults WHERE key = ?", (key,))
        records.append({"key": key, "action": "moved_and_removed", **moving})
    moved = [r for r in records if r["action"] == "moved_and_removed"]
    if moved:
        row = conn.execute(
            "SELECT origin_detail FROM vaults WHERE key = ?", (_V16_MISC_KEY,)
        ).fetchone()
        try:
            detail = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            detail = {"previous_origin_detail": row[0]}
        if not isinstance(detail, dict):
            detail = {"previous_origin_detail": detail}
        history = detail.get("migrated_from")
        if not isinstance(history, list):
            history = []
        at = utc_now()
        history.extend({**r, "at": at} for r in moved)
        detail["migrated_from"] = history
        conn.execute(
            "UPDATE vaults SET origin_detail = ? WHERE key = ?",
            (json.dumps(detail, ensure_ascii=False, sort_keys=True), _V16_MISC_KEY),
        )
    return records


def _v16(conn: sqlite3.Connection) -> None:
    _v16_relax_vault_kind(conn)
    for table in ("episodes", "injections"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN origin_key TEXT")
        conn.execute(
            f"CREATE INDEX {table}_origin_key ON {table}(origin_key)"
            " WHERE origin_key IS NOT NULL"
        )
    absorb_folder_vaults(conn)


# 側載小型機器狀態（v17，D15 UI 已裁決 (a′)；儲存層見 `storage.sidecar`）：
# - 以 (vault, key) 單列覆寫，不留版本、不留墓碑；不進 FTS／向量／快照／recall／list
# - 刻意不設外鍵（同墓碑、匯入對帳清單）：vault 刪除與換 space 由 `storage.admin`
#   在同一交易內一併處理，doctor `sidecar.orphans` 對帳；欄名用 `vault`，
#   `admin.vault_reference_columns` 自動納入換 space 的改名範圍
# - space 冗餘存一份（與 vaults.space 一致）：換 space 時一併改寫，doctor 核對
_V17_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE sidecar_blobs (
        vault   TEXT NOT NULL CHECK (length(trim(vault)) > 0),
        space   TEXT NOT NULL,
        key     TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
        mime    TEXT NOT NULL,
        content BLOB NOT NULL,
        updated TEXT NOT NULL,
        PRIMARY KEY (vault, key)
    ) STRICT
    """,
    "CREATE INDEX sidecar_blobs_space_key ON sidecar_blobs(space, key)",
)


def _v17(conn: sqlite3.Connection) -> None:
    for statement in _V17_STATEMENTS:
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
    _v8,
    _v9,
    _v10,
    _v11,
    _v12,
    _v13,
    _v14,
    _v15,
    _v16,
    _v17,
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
