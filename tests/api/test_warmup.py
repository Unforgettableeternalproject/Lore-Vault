"""啟動暖機：背景呼叫一次 Ollama embed（fake transport），
不阻擋啟動，結果在 /v1/status。"""

from __future__ import annotations

import json
import threading

from lore_vault.enrich.clients import EnrichTimeout, HttpResponse

from .conftest import DIM


class FakeOllama:
    def __init__(self, *, fail: BaseException | None = None, gate=None) -> None:
        self.requests: list[dict] = []
        self.fail = fail
        self.gate = gate

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": json.loads(body), "timeout": timeout})
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        return HttpResponse(200, json.dumps({"embeddings": [[0.5] * DIM]}).encode())


def _warmup(client) -> dict:
    return client.post("/v1/status", json={}).json()["embedding"]["warmup"]


def test_warmup_runs_in_background_with_full_timeout_and_keep_alive(make_client):
    fake = FakeOllama()
    client = make_client(embed_transport=fake, embedding_warmup=True)
    assert client.app.state.lore.warmup.wait(5)
    status = _warmup(client)
    assert status["status"] == "ok"
    assert status["error"] is None
    assert status["elapsed_ms"] is not None and status["finished_at"]
    req = fake.requests[0]
    assert req["url"].endswith("/api/embed")
    # 暖機用完整 embedding.timeout，不是 query_timeout
    assert req["timeout"] == 30.0
    assert req["body"]["keep_alive"] == "30m"


def test_warmup_failure_is_logged_not_fatal(make_client):
    fake = FakeOllama(fail=EnrichTimeout("請求逾時"))
    client = make_client(embed_transport=fake, embedding_warmup=True)
    assert client.app.state.lore.warmup.wait(5)
    body = client.post("/v1/status", json={}).json()
    warm = body["embedding"]["warmup"]
    assert warm["status"] == "failed"
    assert "EnrichTimeout" in warm["error"]
    # 暖機失敗不算不健康；服務照常回應
    assert body["ok"] is True
    assert client.get("/healthz").json() == {"ok": True}


def test_warmup_does_not_block_startup(make_client):
    gate = threading.Event()
    fake = FakeOllama(gate=gate)
    try:
        client = make_client(embed_transport=fake, embedding_warmup=True)
        # 暖機仍卡在 transport 時服務已可回應
        assert _warmup(client)["status"] in {"pending", "running"}
    finally:
        gate.set()
    assert client.app.state.lore.warmup.wait(5)
    assert _warmup(client)["status"] == "ok"


def test_warmup_disabled(make_client):
    fake = FakeOllama()
    client = make_client(embed_transport=fake, embedding_warmup=False)
    assert _warmup(client)["status"] == "disabled"
    assert fake.requests == []


def test_query_embedder_uses_query_timeout_and_keep_alive(make_client):
    """未注入 query_embedder 時，請求路徑也走注入的 transport、
    短逾時、帶 keep_alive。"""
    fake = FakeOllama()
    client = make_client(query_embedder=None, embed_transport=fake)
    client.post("/v1/vaults", json={"key": "folder/w", "display": "w"})
    resp = client.post("/v1/recall", json={"vault": "folder/w", "query": "anything"})
    assert resp.status_code == 200, resp.text
    assert fake.requests, "recall 應呼叫 embedder"
    assert fake.requests[0]["timeout"] == 3.0
    assert fake.requests[0]["body"]["keep_alive"] == "30m"
