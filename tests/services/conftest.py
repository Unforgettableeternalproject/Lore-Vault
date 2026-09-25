"""服務層（recall／notes）測試共用 fixture 與確定性的假 embedder。

測試不打 Ollama：`FakeEmbedder` 把 token 映射到固定維度，
`SYNONYMS` 把中英同義詞映射到同一個維度，模擬跨語言的語意相近。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from lore_vault.schema import Note, Vault
from lore_vault.storage import fts, vectors
from lore_vault.storage.db import connect
from lore_vault.storage.notes import insert_note
from lore_vault.storage.vaults import upsert_vault

DIM = 64
TS = "2026-09-01T00:00:00.000Z"

# 同一組的詞落在同一個維度（模擬 bge-m3 的跨語言語意）
SYNONYMS = {
    "記憶": "memory",
    "檢索": "search",
    "搜尋": "search",
    "search": "search",
    "中文": "chinese",
    "chinese": "chinese",
    "全文": "fulltext",
    "full": "fulltext",
    "text": "fulltext",
    "向量": "vector",
    "vector": "vector",
    "衝突": "conflict",
    "conflict": "conflict",
    "版本": "version",
    "version": "version",
}


def _bucket(token: str) -> int:
    key = SYNONYMS.get(token.lower(), token.lower())
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % DIM


class FakeEmbedder:
    """確定性 bag-of-tokens 向量；`calls` 記錄被呼叫的文字。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        vec = np.zeros(DIM)
        for token in fts.tokens(text):
            vec[_bucket(token)] += 1.0
        if not vec.any():
            vec[0] = 1.0
        return vec.tolist()


class RaisingEmbedder:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        raise self.exc


class ConstantEmbedder:
    def __init__(self, value) -> None:
        self.value = value

    def embed(self, text: str):
        return self.value


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lore.db")
    yield connection
    connection.close()


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def add_vault(conn):
    def add(key: str) -> str:
        vault = Vault(key=key, display=key)
        upsert_vault(conn, vault)
        return vault.key

    return add


@pytest.fixture
def add_note(conn, embedder):
    """新增 note；`embed=True` 時用假 embedder 算 title+body 向量存入。"""

    def add(
        vault: str,
        note_id: str,
        title: str,
        body: str = "",
        *,
        summary: str | None = None,
        topics: tuple[str, ...] = (),
        embed: bool = True,
        ts: str = TS,
    ) -> Note:
        note = insert_note(
            conn,
            vault,
            Note(
                id=note_id,
                vault=vault,
                title=title,
                body=body,
                summary=summary,
                topics=topics,
                created=ts,
                updated=ts,
            ),
        )
        if embed:
            text = f"{title}\n\n{body}" if body else title
            vectors.set_embedding(
                conn, vault, note_id, FakeEmbedder().embed(text), dim=DIM
            )
        return note

    return add
