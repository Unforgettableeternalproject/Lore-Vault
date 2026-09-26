"""CJK bigram FTS5 索引（T-17）。

FTS5 內建 tokenizer 不會切中文：unicode61 把一整段中文當一個 token，
trigram 對 2 字詞靜默回 0 筆（D1 實測）。做法是在 Python 端把索引文字展開：

- CJK 連續段 → overlapping bigram（「記憶系統」→「記憶 憶系 系統」）；
  單一 CJK 字保留為 unigram，否則「用 Python 寫」的「用」會搜不到
- CJK 與拉丁字母交界處斷開（「vault硬過濾」→「vault 硬過 過濾」）
- 其他字元交給 `unicode61 tokenchars '_'`：snake_case 保持完整、大小寫不敏感

查詢語意（`build_match_query`）：
- 每個 token（CJK bigram／拉丁詞）各自成一個 OR 分支，**不要求相鄰**，交給 BM25
  排序：命中越多 token、越集中在標題越前面。中文問句沒有空白，舊作法（以空白切詞、
  詞內 token 組成片語）會把整句變成一個超長片語，lexical 路幾乎必然 0 筆
  （recall-diag 30 題中 17 題），融合退化成向量單路
- 使用者以雙引號括起的片段保留為片語（token 必須相鄰），例如 `"記憶系統"`；
  沒配對的引號當一般字元忽略
- 重複 token（不分大小寫）只留一個；總分支數上限 `MAX_MATCH_TERMS`
- 每個 token 都加雙引號，查詢字串裡的 `* - : ( ) NEAR OR` 等語法一律當字面值
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from .documents import eligible_clause
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


# MATCH 的 OR 分支上限。一般查詢是 7 詞左右的關鍵詞堆疊或一句問句（30 字中文約
# 30 個 bigram），64 足以涵蓋；更長的輸入（例如整段正文）後段 token 對 BM25 排序
# 貢獻很小，卻讓 MATCH 字串與比對成本線性成長。與 notes 查重的
# DEDUP_QUERY_TOKENS（64）一致，查重語意不受此上限影響。
MAX_MATCH_TERMS = 64

# 使用者輸入中成對的雙引號片段
_USER_PHRASE_RE = re.compile(r'"([^"]*)"')


def build_match_query(query: str) -> str | None:
    """把使用者查詢轉成 FTS5 MATCH 字串；沒有可搜尋的 token 時回 None。"""
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        key = term.lower()
        if key not in seen and len(terms) < MAX_MATCH_TERMS:
            seen.add(key)
            terms.append(term)

    # re.split 帶捕獲群組：奇數位置是引號內的片語，偶數位置是其餘文字
    for i, part in enumerate(_USER_PHRASE_RE.split(query)):
        parts = tokens(part)
        if not parts:
            continue
        if i % 2:
            add(_quote(" ".join(parts)))
        else:
            for tok in parts:
                add(_quote(tok))
    if not terms:
        return None
    return " OR ".join(terms)


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
    conn: sqlite3.Connection, vault: str, query: str, *, space: str, limit: int = 20
) -> list[FtsHit]:
    """在指定 vault（或明示 `"*"`）內做 BM25 全文檢索。

    vault 條件與 MATCH 在同一個 SQL 內、LIMIT 之前套用，不會有
    「先取前 N 名再過濾」導致的少回結果。
    """
    scope = resolve_read(conn, vault, space=space)
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


# ── 文件 chunk（T-64）──────────────────────────────────────────────


@dataclass(frozen=True)
class ChunkFtsHit:
    document_id: str
    idx: int
    vault: str
    # -bm25：越大越相關（只在同一次查詢內可比較）
    score: float


def search_chunks(
    conn: sqlite3.Connection, vault: str, query: str, *, space: str, limit: int = 20
) -> list[ChunkFtsHit]:
    """在 vault（或明示 `"*"`）內對可索引文件的 chunk 做 BM25 全文檢索。

    vault／space 條件、可索引條件（ready、未被取代）與 MATCH 在同一個 SQL 內、
    LIMIT 之前套用。
    """
    scope = resolve_read(conn, vault, space=space)
    if limit <= 0:
        raise ValueError(f"limit 必須大於 0，得到 {limit}")
    match = build_match_query(query)
    if match is None:
        return []
    clause, params = vault_clause(scope, "d.vault")
    rows = conn.execute(
        f"""
        SELECT c.document_id, c.idx, d.vault, bm25(chunk_fts) AS rank
        FROM chunk_fts
        JOIN document_chunks c ON c.seq = chunk_fts.rowid
        JOIN documents d ON d.id = c.document_id
        WHERE chunk_fts MATCH ? AND {clause} AND {eligible_clause("d")}
        ORDER BY rank, c.document_id, c.idx
        LIMIT ?
        """,
        (match, *params, limit),
    ).fetchall()
    return [ChunkFtsHit(r[0], int(r[1]), r[2], -float(r[3])) for r in rows]
