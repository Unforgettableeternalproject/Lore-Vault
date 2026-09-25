"""CJK bigram FTS5 索引（T-17）。

FTS5 內建 tokenizer 不會切中文：unicode61 把一整段中文當一個 token，
trigram 對 2 字詞靜默回 0 筆（D1 實測）。做法是在 Python 端把索引文字展開：

- CJK 連續段 → overlapping bigram（「記憶系統」→「記憶 憶系 系統」）；
  單一 CJK 字保留為 unigram，否則「用 Python 寫」的「用」會搜不到
- CJK 與拉丁字母交界處斷開（「vault硬過濾」→「vault 硬過 過濾」）
- 其他字元交給 `unicode61 tokenchars '_'`：snake_case 保持完整、大小寫不敏感

查詢語意（`build_match_query`）：
- 以空白切成「詞」；每個詞用同一套展開規則切成 token，組成 FTS5 片語
  （token 必須相鄰）。所以「記憶系統」要求四個字連續出現，
  `storage/fts.py` 要求 storage、fts、py 連續出現
- 詞與詞之間用 OR，交給 BM25 排序：命中越多詞、越集中在標題越前面。
  實際查詢是平均約 7 詞的關鍵詞堆疊，用 AND 的話一個詞沒命中就 0 筆
- 每個 token 都加雙引號，查詢字串裡的 `* - : ( ) NEAR OR` 等語法一律當字面值
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from .vaults import resolve_read, vault_clause

# CJK 統一表意文字（含擴充 A–F、相容區）、日文假名、韓文音節
_CJK_RANGES = (
    ("぀", "ヿ"),  # 平假名、片假名
    ("㐀", "䶿"),  # 擴充 A
    ("一", "鿿"),  # 基本區
    ("가", "힯"),  # 韓文音節
    ("豈", "﫿"),  # 相容表意文字
    ("\U00020000", "\U0002fa1f"),  # 擴充 B–F、相容補充
)
_CJK_CLASS = "".join(f"{lo}-{hi}" for lo, hi in _CJK_RANGES)
_WORD_RE = re.compile(r"\w+")
_SEGMENT_RE = re.compile(f"([{_CJK_CLASS}]+)|([^{_CJK_CLASS}]+)")

# bm25 欄位權重：title、content
_BM25_WEIGHTS = (2.0, 1.0)


def _cjk_bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokens(text: str) -> list[str]:
    """把文字切成索引 token 序列（未轉小寫；FTS5 unicode61 會處理大小寫）。"""
    result: list[str] = []
    for word in _WORD_RE.findall(text):
        for cjk, other in _SEGMENT_RE.findall(word):
            if cjk:
                result.extend(_cjk_bigrams(cjk))
            else:
                result.append(other)
    return result


def expand(text: str) -> str:
    """存進 FTS 欄位的索引文字。"""
    return " ".join(tokens(text))


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def build_match_query(query: str) -> str | None:
    """把使用者查詢轉成 FTS5 MATCH 字串；沒有可搜尋的 token 時回 None。"""
    phrases: list[str] = []
    seen: set[str] = set()
    for chunk in query.split():
        parts = tokens(chunk)
        if not parts:
            continue
        phrase = _quote(" ".join(parts))
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            phrases.append(phrase)
    if not phrases:
        return None
    return " OR ".join(phrases)


# ── 與 notes 同步（呼叫端負責交易）───────────────────────────────────


def index_text(
    title: str, summary: str | None, body: str, topics: Iterable[str]
) -> tuple[str, str]:
    content = "\n".join([summary or "", body, " ".join(topics)])
    return expand(title), expand(content)


def upsert_row(
    conn: sqlite3.Connection,
    seq: int,
    title: str,
    summary: str | None,
    body: str,
    topics: Iterable[str],
) -> None:
    """寫入／覆蓋一筆 FTS 列。必須在與 notes 相同的交易內呼叫。"""
    title_idx, content_idx = index_text(title, summary, body, topics)
    conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))
    conn.execute(
        "INSERT INTO note_fts (rowid, title, content) VALUES (?, ?, ?)",
        (seq, title_idx, content_idx),
    )


def delete_row(conn: sqlite3.Connection, seq: int) -> None:
    conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))


# ── 查詢 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FtsHit:
    note_id: str
    vault: str
    # -bm25：越大越相關（只在同一次查詢內可比較）
    score: float


def search_notes(
    conn: sqlite3.Connection, vault: str, query: str, *, limit: int = 20
) -> list[FtsHit]:
    """在指定 vault（或明示 `"*"`）內做 BM25 全文檢索。

    vault 條件與 MATCH 在同一個 SQL 內、LIMIT 之前套用，不會有
    「先取前 N 名再過濾」導致的少回結果。
    """
    scope = resolve_read(conn, vault)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    match = build_match_query(query)
    if match is None:
        return []
    clause, params = vault_clause(scope, "n.vault")
    w_title, w_content = _BM25_WEIGHTS
    rows = conn.execute(
        f"""
        SELECT n.id, n.vault, bm25(note_fts, {w_title}, {w_content}) AS rank
        FROM note_fts JOIN notes n ON n.seq = note_fts.rowid
        WHERE note_fts MATCH ? AND {clause}
        ORDER BY rank, n.id
        LIMIT ?
        """,
        (match, *params, limit),
    ).fetchall()
    return [FtsHit(r["id"], r["vault"], -float(r["rank"])) for r in rows]
