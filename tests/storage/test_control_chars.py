"""NUL／控制字元的三層防護：SQLite 實測依據、儲存層拒收、doctor 掃描。

舊 PM 有一則 note 內文夾了真正的 NUL 位元組，整則永久讀不出來、全量列表 500。
這裡先把 SQLite 對含 NUL 的 TEXT 的實際行為寫成測試（防護與 doctor 掃描方式的依據），
再證明：繞過服務層直接呼叫 storage 也寫不進去；直接以 SQL 塞進去 doctor 會紅。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.schema import InvalidCharacters, Note
from lore_vault.storage import checks, enrichment, records
from lore_vault.storage.notes import get_note, insert_note, update_note_if

from .conftest import TS

NUL = "\x00"


# ── SQLite 實測：防護與掃描方式的依據 ───────────────────────────────


@pytest.fixture
def raw():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x TEXT) STRICT")
    conn.execute(f'CREATE VIRTUAL TABLE f USING fts5(x, tokenize = "{fts_tok()}")')
    text = f"abc{NUL}def ghi"
    conn.execute("INSERT INTO t VALUES (?)", (text,))
    conn.execute("INSERT INTO f (rowid, x) VALUES (1, ?)", (text,))
    yield conn, text
    conn.close()


def fts_tok() -> str:
    from lore_vault.storage.migrate import FTS_TOKENIZE

    return FTS_TOKENIZE


def test_python_sqlite3_reads_back_text_with_nul_intact(raw):
    conn, text = raw
    assert conn.execute("SELECT x FROM t").fetchone()[0] == text


def test_sql_text_functions_truncate_at_nul(raw):
    """`length()`、`LIKE`、`substr()` 都停在 NUL。

    SQL 端不能靠文字函式看含 NUL 的資料。"""
    conn, text = raw
    length, blob_length, like, prefix = conn.execute(
        "SELECT length(x), length(CAST(x AS BLOB)), x LIKE '%def%', substr(x, 1, 20) "
        "FROM t"
    ).fetchone()
    assert len(text) == 11
    assert length == 3  # 只算到 NUL 前
    assert blob_length == 11
    assert like == 0  # NUL 之後的內容 LIKE 看不到
    assert prefix == "abc"


def test_blob_cast_finds_nul_reliably(raw):
    conn, _ = raw
    position = conn.execute("SELECT instr(CAST(x AS BLOB), X'00') FROM t").fetchone()
    assert position[0] == 4


def test_fts5_indexes_tokens_on_both_sides_of_nul(raw):
    conn, text = raw
    for token in ("abc", "def", "ghi"):
        hits = conn.execute("SELECT rowid FROM f WHERE f MATCH ?", (token,)).fetchall()
        assert hits == [(1,)], token
    assert conn.execute("SELECT x FROM f").fetchone()[0] == text


def test_json1_rejects_raw_nul_but_json_dumps_escapes_it():
    conn = sqlite3.connect(":memory:")
    try:
        assert (
            conn.execute("SELECT json_valid(?)", ('{"a":"x\x00y"}',)).fetchone()[0] == 0
        )
        escaped = json.dumps({"a": "x\x00y\x08\x0c"}, ensure_ascii=False)
        assert "\\u0000" in escaped and "\\b" in escaped and "\\f" in escaped
        assert conn.execute("SELECT json_valid(?)", (escaped,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_python_sqlite3_cannot_store_lone_surrogate():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (x TEXT)")
        with pytest.raises(UnicodeEncodeError):
            conn.execute("INSERT INTO t VALUES (?)", ("a\ud800",))
    finally:
        conn.close()


# ── 儲存層拒收（繞過服務層也擋得住）─────────────────────────────────


def _note(**overrides) -> Note:
    data = {
        "id": "n-1",
        "vault": "folder/cc",
        "title": "標題",
        "body": "正文",
        "created": TS,
        "updated": TS,
    }
    data.update(overrides)
    data.setdefault("principal", "xavier")
    return Note(**data)


@pytest.mark.parametrize(
    ("overrides", "field", "index"),
    [
        ({"title": f"ab{NUL}c"}, "title", 2),
        ({"body": f"x{NUL}"}, "body", 1),
        ({"body": "a\x1bb"}, "body", 1),
        ({"body": "a\ud800"}, "body", 1),
        ({"topics": ("ok", f"t{NUL}")}, "topics[1]", 1),
        ({"links": (f"{NUL}l",)}, "links[0]", 0),
        ({"supersedes": f"n{NUL}2"}, "supersedes", 1),
    ],
)
def test_insert_note_rejects_forbidden_characters(
    conn, add_vault, overrides, field, index
):
    add_vault("folder/cc")
    with pytest.raises(InvalidCharacters) as info:
        insert_note(conn, "folder/cc", _note(**overrides), space="dev")
    assert (info.value.field, info.value.index) == (field, index)
    # 訊息只有欄位、位置、碼位，不回顯內容
    assert "ab" not in str(info.value) and "正文" not in str(info.value)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_insert_note_allows_tab_newline_carriage_return(conn, add_vault):
    add_vault("folder/cc")
    stored = insert_note(conn, "folder/cc", _note(body="a\tb\nc\r\nd"), space="dev")
    assert get_note(conn, "folder/cc", stored.id, space="dev").body == "a\tb\nc\r\nd"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": f"t{NUL}"},
        {"body": "b\x07"},
        {"summary": f"s{NUL}"},
        {"topics": (f"x{NUL}",)},
        {"links": ("a\udc00",)},
    ],
)
def test_update_note_if_rejects_forbidden_characters(conn, add_vault, changes):
    add_vault("folder/cc")
    stored = insert_note(conn, "folder/cc", _note(), space="dev")
    with pytest.raises(InvalidCharacters):
        update_note_if(
            conn, "folder/cc", stored.id, stored.updated, changes, space="dev"
        )
    after = get_note(conn, "folder/cc", stored.id, space="dev")
    assert after == stored


def test_llm_summary_is_sanitized_not_rejected(conn, add_vault):
    """摘要是衍生文字：清理而非拒收（拒收只會讓補算無限重試、doctor 永遠紅）。"""
    add_vault("folder/cc")
    stored = insert_note(conn, "folder/cc", _note(), space="dev")
    seq = conn.execute("SELECT seq FROM notes WHERE id = ?", (stored.id,)).fetchone()[0]
    assert enrichment.write_summary_if_current(conn, seq, stored.updated, f"摘{NUL}要")
    assert get_note(conn, "folder/cc", stored.id, space="dev").summary == "摘\\0要"
    assert checks.control_chars(conn).status == "pass"


# ── doctor storage.control_chars ─────────────────────────────────────


@pytest.fixture
def clean(conn, add_vault, add_note):
    v = add_vault("folder/cc")
    add_note(v, "n-1", "標題", "正文\t縮排\n換行", summary="摘要", topics=("t",))
    # 字面上的反斜線序列不是控制字元（粗篩會命中，Python 確認後排除）
    add_note(v, "n-2", "字面 \\u0000 與 \\b", "路徑 C:\\new\\folder", topics=("\\f",))
    return conn


def _doctor(conn) -> dict[str, Status]:
    report = default_registry().run(
        DoctorContext(settings={"embedding_dim": 4}, resources={"db": conn}),
        categories=["storage"],
    )
    return {o.name: o.result.status for o in report.outcomes}


def test_clean_data_passes(clean):
    rec = checks.control_chars(clean)
    assert rec.status == "pass", rec.details
    assert _doctor(clean)["storage.control_chars"] is Status.PASS


@pytest.mark.parametrize("column", ["title", "body", "summary"])
def test_raw_nul_injected_by_sql_goes_red(clean, column):
    clean.execute(f"UPDATE notes SET {column} = 'a' || char(0) || 'b' WHERE id = 'n-1'")
    rec = checks.control_chars(clean)
    assert rec.status == "fail"
    assert rec.counts["notes"] == 1
    assert rec.details == (f"note n-1：{column}",)
    assert _doctor(clean)["storage.control_chars"] is Status.FAIL


def test_other_c0_byte_injected_by_sql_goes_red(clean):
    clean.execute("UPDATE notes SET body = 'a' || char(27) || 'b' WHERE id = 'n-2'")
    assert checks.control_chars(clean).details == ("note n-2：body",)


def test_escaped_control_char_in_topics_json_goes_red(clean):
    clean.execute(
        "UPDATE notes SET topics = ? WHERE id = 'n-1'", (json.dumps(["a\x00"]),)
    )
    rec = checks.control_chars(clean)
    assert rec.status == "fail" and rec.details == ("note n-1：topics",)


def _episode_data(**overrides) -> dict:
    data = {
        "prompt_id": "p-1",
        "turn_index": 0,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "machine": "m",
        "started_at": None,
        "ended_at": None,
        "cwd": [],
        "repo": None,
        "repo_root": None,
        "git_branch": [],
        "cc_version": None,
        "user_text": "u",
        "assistant_text": "a",
        "tool_sequence": [],
        "tool_calls_total": 0,
        "mcp_tools": [],
        "skills": [],
        "files_edited": [],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
    }
    data.update(overrides)
    return data


def test_episode_and_concept_data_are_scanned(clean):
    from lore_vault.schema import Concept, Episode

    records.insert_episode(
        clean, "folder/cc", Episode.from_dict(_episode_data(user_text="字面 \\u0000"))
    )
    records.upsert_concept(
        clean, "folder/cc", Concept(id="c-1", statement="s", kind=None)
    )
    assert checks.control_chars(clean).status == "pass"

    bad = json.dumps(_episode_data(assistant_text="x\x08y"), ensure_ascii=False)
    clean.execute("UPDATE episodes SET data = ? WHERE session_id = 's-1'", (bad,))
    clean.execute(
        "UPDATE concepts SET data = json_set(data, '$.why', 'a' || char(1)) "
        "WHERE id = 'c-1'"
    )
    rec = checks.control_chars(clean)
    assert rec.status == "fail"
    assert rec.counts["episodes"] == 1 and rec.counts["concepts"] == 1
    assert "episode s-1/p-1/0：data" in rec.details
    assert "concept c-1：data" in rec.details


def test_raw_control_byte_inside_json_counts_as_finding(clean):
    """JSON 內被直接塞進原始控制字元（json.loads 會拒絕解析）也算問題。"""
    clean.execute(
        "UPDATE notes SET topics = '[\"a' || char(0) || '\"]' WHERE id = 'n-1'"
    )
    assert checks.control_chars(clean).details == ("note n-1：topics",)
