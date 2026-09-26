"""T-17：CJK bigram FTS5 索引與查詢語意。"""

from __future__ import annotations

import pytest

from lore_vault.storage import fts, notes

V = "folder/fts"


def test_tokens_split_cjk_into_overlapping_bigrams():
    assert fts.tokens("記憶系統") == ["記憶", "憶系", "系統"]
    assert fts.tokens("記憶") == ["記憶"]
    # 單一 CJK 字保留 unigram，否則搜不到
    assert fts.tokens("用 Python 寫") == ["用", "Python", "寫"]
    # CJK 與拉丁字母交界處斷開
    assert fts.tokens("vault硬過濾") == ["vault", "硬過", "過濾"]
    assert fts.tokens("recall_budget RecallService") == [
        "recall_budget",
        "RecallService",
    ]
    assert fts.tokens("storage/fts.py") == ["storage", "fts", "py"]
    assert fts.tokens("「記憶」，系統。") == ["記憶", "系統"]


def test_match_query_quotes_everything_and_uses_or():
    assert fts.build_match_query("記憶系統 SQLite") == '"記憶 憶系 系統" OR "SQLite"'
    assert fts.build_match_query("   ") is None
    assert fts.build_match_query("*** -- ()") is None
    # FTS5 語法字元與保留字只當字面值
    q = fts.build_match_query('NEAR(a b) OR "x" -y col:z')
    assert q == '"NEAR a" OR "b" OR "OR" OR "x" OR "y" OR "col z"'
    # 重複詞（大小寫不同）只留一個
    assert fts.build_match_query("Vault vault") == '"Vault"'


@pytest.fixture
def corpus(conn, add_vault, add_note):
    add_vault(V)
    add_note(
        V,
        "n-memory",
        "記憶系統設計",
        "Lore Vault 的記憶層用 SQLite 與 WAL。RecallService 負責 recall_budget 裁切。"
        "檔案在 src/lore_vault/storage/fts.py。",
    )
    add_note(V, "n-vector", "向量暴力比對", "NumPy 點積，不建 ANN 索引。")
    add_note(V, "n-time", "時間戳格式", "一律 ISO-8601 UTC，用 Z 結尾。")
    add_note(V, "n-weak", "雜記", "提到記憶一次。")
    return conn


def _ids(conn, query, **kw):
    return [h.note_id for h in fts.search_notes(conn, V, query, space="dev", **kw)]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("記憶系統", "n-memory"),  # 4 字，含標題
        ("暴力比對", "n-vector"),  # 4 字
        ("時間戳", "n-time"),  # 3 字
        ("RecallService", "n-memory"),  # CamelCase
        ("recallservice", "n-memory"),  # 大小寫不敏感
        ("recall_budget", "n-memory"),  # snake_case 保持完整
        ("storage/fts.py", "n-memory"),  # 路徑片段
        ("lore_vault/storage", "n-memory"),
        ("ISO-8601", "n-time"),
    ],
)
def test_single_term_hits(corpus, query, expected):
    assert _ids(corpus, query) == [expected]


def test_two_char_cjk_word_hits(corpus):
    """D1：trigram 對 2 字詞回 0 筆；bigram 必須命中。"""
    assert set(_ids(corpus, "記憶")) == {"n-memory", "n-weak"}


def test_cjk_term_is_a_phrase_not_scattered_chars(corpus):
    # 「記憶」與「設計」都在 n-memory 的標題，但「記憶設計」並不連續出現
    assert _ids(corpus, "記憶設計") == []


def test_snake_case_identifier_is_not_split(corpus):
    # tokenchars '_'：recall_budget 是一個 token，單獨的 recall 不會命中它
    assert _ids(corpus, "recall") == []


def test_multi_term_mixed_query_is_or_ranked_by_bm25(corpus):
    # 含一個全庫都沒有的詞：用 AND 會 0 筆
    ids = _ids(corpus, "記憶 SQLite WAL 不存在的詞 nonexistent")
    assert ids[0] == "n-memory"
    assert "n-weak" in ids
    # 命中越多詞排越前面
    assert ids.index("n-memory") < ids.index("n-weak")


def test_mixed_cjk_latin_keyword_stack(corpus):
    ids = _ids(corpus, "向量 NumPy ANN 記憶")
    assert ids[0] == "n-vector"
    assert set(ids) == {"n-vector", "n-memory", "n-weak"}


def test_empty_or_syntax_only_query_returns_nothing(corpus):
    assert _ids(corpus, "") == []
    assert _ids(corpus, '*** "" ()') == []


def test_limit_and_score_order(corpus):
    hits = fts.search_notes(corpus, V, "記憶", space="dev", limit=1)
    assert len(hits) == 1
    all_hits = fts.search_notes(corpus, V, "記憶", space="dev")
    assert [h.score for h in all_hits] == sorted(
        (h.score for h in all_hits), reverse=True
    )
    with pytest.raises(ValueError):
        fts.search_notes(corpus, V, "記憶", space="dev", limit=0)


# ── 與主表同步（同一交易）───────────────────────────────────────────


def test_update_reindexes_and_delete_removes(corpus):
    note = notes.get_note(corpus, V, "n-time", space="dev")
    notes.update_note_if(
        corpus,
        V,
        "n-time",
        note.updated,
        {"title": "時區規則", "body": "容器是 UTC"},
        space="dev",
    )
    assert _ids(corpus, "時間戳") == []
    assert _ids(corpus, "時區規則") == ["n-time"]
    notes.delete_note(corpus, V, "n-time", space="dev")
    assert _ids(corpus, "時區規則") == []
    assert corpus.execute("SELECT count(*) FROM note_fts").fetchone()[0] == 3


def test_summary_and_topics_are_indexed(conn, add_vault, add_note):
    add_vault(V)
    add_note(V, "n-1", "標題", "正文", summary="摘要提到快照", topics=("backup_plan",))
    assert _ids(conn, "快照") == ["n-1"]
    assert _ids(conn, "backup_plan") == ["n-1"]


def test_fts_failure_rolls_back_note_insert(conn, add_vault, add_note, monkeypatch):
    add_vault(V)

    def boom(*args, **kwargs):
        raise RuntimeError("FTS 寫入失敗")

    monkeypatch.setattr(fts, "upsert_row", boom)
    with pytest.raises(RuntimeError):
        add_note(V, "n-1", "標題")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert not conn.in_transaction


def test_fts_failure_rolls_back_note_update(corpus, monkeypatch):
    before = notes.get_note(corpus, V, "n-time", space="dev")

    def boom(*args, **kwargs):
        raise RuntimeError("FTS 寫入失敗")

    monkeypatch.setattr(fts, "upsert_row", boom)
    with pytest.raises(RuntimeError):
        notes.update_note_if(
            corpus, V, "n-time", before.updated, {"title": "新標題"}, space="dev"
        )
    assert notes.get_note(corpus, V, "n-time", space="dev") == before
