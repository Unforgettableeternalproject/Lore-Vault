"""HTTP 層測試共用：隔離的資料庫與設定、確定性假 embedder、帶 token 的 TestClient。

不打網路、不讀 os.environ（設定一律以參數注入），不啟動長駐服務。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, EmbeddingConfig, Secret
from lore_vault.storage import fts, vectors
from lore_vault.storage.db import connect

DIM = 16
TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def fake_vector(text: str) -> list[float]:
    """bag-of-tokens 向量：共用詞越多 cosine 越高。"""
    vec = np.zeros(DIM)
    for token in fts.tokens(text):
        digest = hashlib.sha1(token.lower().encode("utf-8")).hexdigest()
        vec[int(digest, 16) % DIM] += 1.0
    if not vec.any():
        vec[0] = 1.0
    return vec.tolist()


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return fake_vector(text)


class RaisingEmbedder:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def embed(self, text: str) -> list[float]:
        raise self.exc


def make_settings(db_path: Path, **overrides) -> ApiSettings:
    base = {
        "db_path": db_path,
        "token": Secret(TOKEN),
        "config": Config(embedding=EmbeddingConfig(dim=DIM)),
        "query_embedder": FakeEmbedder(),
        "enrich_worker": False,
    }
    base.update(overrides)
    return ApiSettings(**base)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "lore.db"


@pytest.fixture
def client(db_path):
    with TestClient(create_app(make_settings(db_path))) as c:
        c.headers.update(AUTH)
        yield c


@pytest.fixture
def make_client(db_path):
    """自訂設定的 client；離開測試時關閉（觸發 lifespan shutdown）。"""
    opened: list[TestClient] = []

    def make(**overrides) -> TestClient:
        c = TestClient(create_app(make_settings(db_path, **overrides)))
        c.__enter__()
        c.headers.update(AUTH)
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def create_vault(client: TestClient, key: str, **extra) -> dict:
    resp = client.post("/v1/vaults", json={"key": key, "display": key, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def write_note(client: TestClient, vault: str, title: str, body: str, **extra) -> dict:
    resp = client.post(
        "/v1/write", json={"vault": vault, "title": title, "body": body, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def embed_all(db_path: Path) -> None:
    """模擬背景補算：替所有 note 存入 title+body 的假向量。"""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id, vault, title, body FROM notes").fetchall()
        for row in rows:
            text = f"{row['title']}\n\n{row['body']}" if row["body"] else row["title"]
            vectors.set_embedding(
                conn, row["vault"], row["id"], fake_vector(text), dim=DIM
            )
    finally:
        conn.close()
