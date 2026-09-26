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


def test_match_query_quotes_every_token_and_uses_or():
    # 每個 token 各自一個 OR 分支，不組成片語（不要求相鄰）
    assert (
        fts.build_match_query("記憶系統 SQLite")
        == '"記憶" OR "憶系" OR "系統" OR "SQLite"'
    )
    assert fts.build_match_query("   ") is None
    assert fts.build_match_query("*** -- ()") is None
    # FTS5 語法字元與保留字只當字面值；成對引號內保留為片語
    q = fts.build_match_query('NEAR(a b) OR "x" -y col:z')
    assert q == '"NEAR" OR "a" OR "b" OR "OR" OR "x" OR "y" OR "col" OR "z"'
    # 重複 token（大小寫不同）只留一個
    assert fts.build_match_query("Vault vault") == '"Vault"'


def test_pure_cjk_question_is_not_one_long_phrase():
    """中文問句沒有空白：舊實作會組成一整個片語，幾乎必然 0 筆。"""
    q = fts.build_match_query("資料庫要怎麼備份")
    assert q == '"資料" OR "料庫" OR "庫要" OR "要怎" OR "怎麼" OR "麼備" OR "備份"'
    # 每個分支都是單一 token，沒有任何含空白的片語
    assert all(" " not in term.strip('"') for term in q.split(" OR "))


def test_mixed_cjk_latin_query_splits_per_token():
    assert (
        fts.build_match_query("用SQLite做WAL備份")
        == '"用" OR "SQLite" OR "做" OR "WAL" OR "備份"'
    )


def test_user_quoted_phrase_is_kept_as_phrase():
    assert fts.build_match_query('"記憶系統" 設計') == '"記憶 憶系 系統" OR "設計"'
    assert (
        fts.build_match_query('找 "storage/fts.py" 檔案')
        == '"找" OR "storage fts py" OR "檔案"'
    )
    # 片語與同一個單 token 去重
    assert fts.build_match_query('"vault" Vault') == '"vault"'
    # 空片語、只有符號的片語被略過
    assert fts.build_match_query('"" "***" 記憶') == '"記憶"'


def test_unbalanced_or_injected_quotes_are_literal():
    # 沒配對的引號當一般字元忽略，不會讓 MATCH 語法失衡
    assert fts.build_match_query('記憶"系統') == '"記憶" OR "系統"'
    # 成對引號框住的語法字當片語字面值，不會變成 FTS 運算子
    assert fts.build_match_query('a" OR b NEAR "c') == '"a" OR "OR b NEAR" OR "c"'
    # 引號內的 FTS 語法仍只當字面 token
    assert fts.build_match_query('"a* OR -b"') == '"a OR b"'


def test_match_terms_are_capped():
    words = [f"w{i}" for i in range(fts.MAX_MATCH_TERMS + 10)]
    q = fts.build_match_query(" ".join(words))
    terms = q.split(" OR ")
    assert len(terms) == fts.MAX_MATCH_TERMS
    assert terms[0] == '"w0"'
    assert terms[-1] == f'"w{fts.MAX_MATCH_TERMS - 1}"'


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
        ('"記憶系統"', "n-memory"),  # 4 字片語，含標題
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


def test_unquoted_cjk_term_matches_partial_bigrams(corpus):
    # 「記憶」與「設計」都在 n-memory 的標題，但「記憶設計」並不連續出現：
    # 不加引號時照樣命中（逐 token OR），命中較多的排前面
    ids = _ids(corpus, "記憶設計")
    assert ids[0] == "n-memory"
    assert set(ids) == {"n-memory", "n-weak"}
    assert _ids(corpus, "記憶系統")[0] == "n-memory"


def test_quoted_cjk_term_requires_adjacency(corpus):
    assert _ids(corpus, '"記憶設計"') == []
    assert _ids(corpus, '"記憶系統"') == ["n-memory"]


def test_injection_and_quote_heavy_queries_do_not_raise(corpus):
    for q in ('"', '""', '"記憶', 'a" OR "b', 'NEAR("記憶" "系統", 2)', "記憶* ^系統"):
        _ids(corpus, q)  # 不可拋 sqlite3.OperationalError


def test_pure_cjk_question_recalls_note_with_some_of_its_words(
    conn, add_vault, add_note
):
    """召回回歸：無空白的中文問句只要含 note 的部分詞就要命中。

    舊實作把整句當成一個片語（要求每個 bigram 相鄰），這題回 0 筆。
    """
    add_vault(V)
    add_note(V, "n-backup", "備份排程", "每天凌晨用 VACUUM INTO 把資料庫備份到主機。")
    add_note(V, "n-other", "時區規則", "容器一律 UTC。")
    assert _ids(conn, "資料庫要怎麼定期備份到主機上") == ["n-backup"]


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
