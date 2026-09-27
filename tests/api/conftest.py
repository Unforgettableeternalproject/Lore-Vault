"""HTTP 層測試共用：隔離的資料庫與設定、確定性假 embedder、帶 token 的 TestClient。

不打網路、不讀 os.environ（設定一律以參數注入），不啟動長駐服務。

`client`／`make_client` 對需要 space 的 `/v1/*` 端點自動補 `"space": "dev"`
（模擬 MCP 殼的注入）；body 已帶 space 則不動。要測「漏帶 space」時傳
`json={..., "space": OMIT}`，送出前會把該鍵拿掉。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, EmbeddingConfig, EpisodesConfig, Secret
from lore_vault.storage import fts, ui_login, vectors
from lore_vault.storage.db import connect

DIM = 16
SPACED_PATHS = frozenset(
    {
        "/v1/vault_resolve",
        "/v1/vaults",
        "/v1/recall",
        "/v1/ask",
        "/v1/get",
        "/v1/list",
        "/v1/write",
        "/v1/update",
        "/v1/status",
    }
)
OMIT = object()


class SpaceClient(TestClient):
    """模擬殼：對 space 必填的端點自動帶目前 space（預設 dev）。"""

    space = "dev"

    def post(self, url, *args, **kwargs):  # type: ignore[override]
        body = kwargs.get("json")
        if isinstance(body, dict) and str(url) in SPACED_PATHS:
            body = dict(body)
            if body.get("space") is OMIT:
                del body["space"]
            else:
                body.setdefault("space", self.space)
            kwargs["json"] = body
        return super().post(url, *args, **kwargs)


TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# UI 帳號（A23）：測試用固定值；scrypt 用便宜參數（參數存在列中，驗證照列中參數）
UI_USER = "UEPBernie"
UI_DISPLAY = "Xavier (Bernie)"
UI_PASSWORD = "correct horse battery"
UI_LOGIN = {"username": UI_USER, "password": UI_PASSWORD}
CHEAP_SCRYPT = ui_login.ScryptParams(n=2**10, r=8, p=1)


def seed_ui_account(
    db_path: Path,
    username: str = UI_USER,
    password: str = UI_PASSWORD,
    display: str | None = UI_DISPLAY,
) -> None:
    """在（會被遷移的）測試資料庫建立 UI 帳號；app 啟動前後呼叫都可以。"""
    conn = connect(db_path)
    try:
        ui_login.set_password(
            conn,
            username,
            password,
            now=datetime.now(UTC),
            display=display,
            params=CHEAP_SCRYPT,
        )
    finally:
        conn.close()


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
        # episode 收料預設關閉（D13）；既有測試照常收料，關閉的行為另有測試
        "config": Config(
            embedding=EmbeddingConfig(dim=DIM), episodes=EpisodesConfig(ingest=True)
        ),
        "query_embedder": FakeEmbedder(),
        "enrich_worker": False,
        # 預設不暖機：否則背景執行緒會去連本機 Ollama（暖機另有測試）
        "embedding_warmup": False,
    }
    base.update(overrides)
    return ApiSettings(**base)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "lore.db"


@pytest.fixture
def client(db_path):
    with SpaceClient(create_app(make_settings(db_path))) as c:
        c.headers.update(AUTH)
        yield c


@pytest.fixture
def make_client(db_path):
    """自訂設定的 client；離開測試時關閉（觸發 lifespan shutdown）。"""
    opened: list[TestClient] = []

    def make(**overrides) -> TestClient:
        c = SpaceClient(create_app(make_settings(db_path, **overrides)))
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
        rows = conn.execute(
            "SELECT n.id, n.vault, n.title, n.body, v.space FROM notes n "
            "JOIN vaults v ON v.key = n.vault"
        ).fetchall()
        for row in rows:
            text = f"{row['title']}\n\n{row['body']}" if row["body"] else row["title"]
            vectors.set_embedding(
                conn,
                row["vault"],
                row["id"],
                fake_vector(text),
                space=row["space"],
                dim=DIM,
            )
    finally:
        conn.close()
