"""請求路徑的冷啟動逾時：Ollama `/api/ps` 回報模型未載入時，查詢 embedding 改用
`embedding.cold_query_timeout`（預設 20 秒），不因 3 秒短逾時而降級；已載入維持短逾時；
`/api/ps` 探測失敗時退回短逾時。`/v1/status` 回報 `embedding.model_loaded`。
`/v1/ask` 不論模型是否載入一律用 `cold_query_timeout`。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from lore_vault.api.state import HOT_WINDOW, QueryEmbedder
from lore_vault.ask.client import Completion
from lore_vault.config import load_config
from lore_vault.enrich.clients import (
    EnrichTimeout,
    HttpResponse,
    OllamaEmbedder,
    ollama_model_loaded,
)

from .conftest import DIM, create_vault, embed_all, write_note

MODEL = "bge-m3"


class FakeOllama:
    """模擬 Ollama：`loaded` 決定 `/api/ps` 的內容；模型未載入時 embed 要
    `load_seconds` 才回應（逾時小於它就丟 EnrichTimeout，等同真實的冷啟動逾時）。"""

    def __init__(
        self,
        *,
        loaded: bool,
        load_seconds: float = 5.0,
        ps=None,
        slow_seconds: float = 0.0,
    ) -> None:
        self.loaded = loaded
        self.load_seconds = load_seconds
        self.ps = ps  # 自訂 /api/ps 行為：例外物件或 HttpResponse
        # 已載入時 embed 仍要這麼久（偶發卡頓）；逾時小於它就丟 EnrichTimeout
        self.slow_seconds = slow_seconds
        self.embeds: list[float] = []
        self.probes = 0

    def __call__(self, url, body, headers, timeout):
        if url.endswith("/api/ps"):
            self.probes += 1
            assert body is None, "/api/ps 應以 GET 呼叫"
            if isinstance(self.ps, BaseException):
                raise self.ps
            if isinstance(self.ps, HttpResponse):
                return self.ps
            models = [{"name": f"{MODEL}:latest", "model": f"{MODEL}:latest"}]
            payload = {"models": models if self.loaded else []}
            return HttpResponse(200, json.dumps(payload).encode())
        assert url.endswith("/api/embed")
        json.loads(body)
        self.embeds.append(timeout)
        if not self.loaded and timeout < self.load_seconds:
            raise EnrichTimeout(f"請求逾時（{timeout} 秒）：{url}")
        if self.loaded and timeout < self.slow_seconds:
            raise EnrichTimeout(f"請求逾時（{timeout} 秒）：{url}")
        self.loaded = True  # 載入完成後留在記憶體
        return HttpResponse(200, json.dumps({"embeddings": [[0.5] * DIM]}).encode())


def _recall(client) -> dict:
    client.post("/v1/vaults", json={"key": "folder/cold", "display": "cold"})
    resp = client.post("/v1/recall", json={"vault": "folder/cold", "query": "anything"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_cold_model_uses_long_timeout_and_is_not_degraded(make_client):
    fake = FakeOllama(loaded=False)
    client = make_client(query_embedder=None, embed_transport=fake)
    body = _recall(client)
    assert body["degraded"] is False, body
    assert fake.probes == 1
    assert fake.embeds == [20.0]


def test_loaded_model_keeps_short_timeout(make_client):
    fake = FakeOllama(loaded=True)
    client = make_client(query_embedder=None, embed_transport=fake)
    assert _recall(client)["degraded"] is False
    assert fake.embeds == [3.0]


@pytest.mark.parametrize(
    "ps",
    [
        EnrichTimeout("請求逾時（1.0 秒）"),
        HttpResponse(500, b"boom"),
        HttpResponse(200, b"not json"),
    ],
)
def test_probe_failure_falls_back_to_short_timeout(make_client, ps):
    """探測失敗＝狀況不明：維持短逾時（不把請求拖長），冷的話照舊降級。"""
    fake = FakeOllama(loaded=False, ps=ps)
    client = make_client(query_embedder=None, embed_transport=fake)
    body = _recall(client)
    assert fake.embeds == [3.0]
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embedder_timeout"


class _Answerer:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> Completion:
        self.calls += 1
        return Completion(
            json.dumps({"status": "insufficient", "points": []}), "fake", {}
        )


@pytest.fixture
def slow_hot(make_client, db_path):
    """模型已載入，但 embed 要 5 秒（> query_timeout 3、< cold_query_timeout 20）。
    note 先用預設假 embedder 寫好並補向量，避免寫入查重動到 FakeOllama。"""
    setup = make_client()
    create_vault(setup, "folder/cold")
    write_note(setup, "folder/cold", "資料庫選型", "決定採用 SQLite WAL。")
    embed_all(db_path)
    fake = FakeOllama(loaded=True, slow_seconds=5.0)
    answerer = _Answerer()
    client = make_client(query_embedder=None, embed_transport=fake, answerer=answerer)
    return client, fake, answerer


def test_ask_always_uses_cold_timeout_even_when_loaded(slow_hot):
    """ask 本身要等問答模型數秒：已載入也用 cold_query_timeout，不因 3 秒短逾時降級。"""
    client, fake, answerer = slow_hot
    resp = client.post("/v1/ask", json={"vault": "folder/cold", "question": "SQLite"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is False, body
    assert fake.embeds == [20.0]
    assert answerer.calls == 1


def test_recall_keeps_short_timeout_when_loaded(slow_hot):
    """同情境的 recall 維持原行為：已載入用短逾時，卡住就降級。"""
    client, fake, _ = slow_hot
    resp = client.post("/v1/recall", json={"vault": "folder/cold", "query": "SQLite"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert fake.embeds == [3.0]
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embedder_timeout"


def test_recent_success_skips_probe():
    fake = FakeOllama(loaded=True)
    now = [1000.0]
    cfg = load_config(environ={}).embedding
    short = OllamaEmbedder(
        dataclasses.replace(cfg, timeout=3.0, dim=DIM), transport=fake
    )
    cold = OllamaEmbedder(
        dataclasses.replace(cfg, timeout=20.0, dim=DIM), transport=fake
    )
    probe_calls = []

    def probe():
        probe_calls.append(1)
        return ollama_model_loaded(cfg.base_url, MODEL, transport=fake, timeout=1.0)

    qe = QueryEmbedder(short, cold=cold, probe=probe, clock=lambda: now[0])
    qe.embed("a")
    qe.embed("b")
    assert len(probe_calls) == 1  # 第二次在 HOT_WINDOW 內，不探測
    now[0] += HOT_WINDOW + 1
    qe.embed("c")
    assert len(probe_calls) == 2
    assert fake.embeds == [3.0, 3.0, 3.0]


def test_status_reports_model_loaded(make_client):
    cold = make_client(query_embedder=None, embed_transport=FakeOllama(loaded=False))
    assert cold.post("/v1/status", json={}).json()["embedding"]["model_loaded"] is False
    hot = make_client(query_embedder=None, embed_transport=FakeOllama(loaded=True))
    assert hot.post("/v1/status", json={}).json()["embedding"]["model_loaded"] is True
    unknown = make_client(
        query_embedder=None,
        embed_transport=FakeOllama(loaded=False, ps=EnrichTimeout("逾時")),
    )
    body = unknown.post("/v1/status", json={}).json()
    assert body["embedding"]["model_loaded"] is None


@pytest.mark.parametrize(
    ("configured", "listed", "expected"),
    [
        ("bge-m3", "bge-m3:latest", True),
        ("bge-m3", "bge-m3", True),
        ("bge-m3:latest", "bge-m3:latest", True),
        ("bge-m3", "bge-m3-large:latest", False),
        ("bge-m3:q8", "bge-m3:latest", False),
    ],
)
def test_model_name_matching(configured, listed, expected):
    def transport(url, body, headers, timeout):
        return HttpResponse(200, json.dumps({"models": [{"name": listed}]}).encode())

    assert (
        ollama_model_loaded("http://x", configured, transport=transport, timeout=1.0)
        is expected
    )


def test_cold_query_timeout_setting():
    assert load_config(environ={}).embedding.cold_query_timeout == 20.0
    cfg = load_config(environ={"LORE_VAULT_EMBEDDING_COLD_QUERY_TIMEOUT": "45"})
    assert cfg.embedding.cold_query_timeout == 45.0
