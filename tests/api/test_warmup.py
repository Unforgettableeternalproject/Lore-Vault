"""啟動暖機：背景呼叫 Ollama embed（fake transport），不阻擋啟動，結果在 /v1/status。
連線類失敗（Ollama 尚未就緒）退避重試，其他錯誤直接 failed。"""

from __future__ import annotations

import json
import threading

from lore_vault.api.warmup import EmbeddingWarmup
from lore_vault.enrich.clients import (
    EnrichTimeout,
    HttpResponse,
    InvalidOutput,
    ProviderUnavailable,
)

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
    # 非連線類錯誤：不重試，直接 failed
    fake = FakeOllama(fail=InvalidOutput("回應格式錯"))
    client = make_client(embed_transport=fake, embedding_warmup=True)
    assert client.app.state.lore.warmup.wait(5)
    body = client.post("/v1/status", json={}).json()
    warm = body["embedding"]["warmup"]
    assert warm["status"] == "failed"
    assert "InvalidOutput" in warm["error"]
    assert warm["attempts"] == 1
    assert len(fake.requests) == 1
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


class ScriptedEmbedder:
    """依序丟出預排的例外，用完回傳向量。"""

    def __init__(self, *failures: BaseException, vector=(0.5,)) -> None:
        self.failures = list(failures)
        self.vector = list(vector)
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.vector


class FakeClock:
    """注入的等待不真的睡：推進假時鐘並記下每次等待秒數。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        self.statuses: list[dict] = []
        self.warmup: EmbeddingWarmup | None = None

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        if self.warmup is not None:
            self.statuses.append(self.warmup.status())
        self.waits.append(seconds)
        self.now += seconds
        return False


def _run(embedder, **kwargs) -> tuple[EmbeddingWarmup, FakeClock]:
    fc = FakeClock()
    warmup = EmbeddingWarmup(embedder, wait=fc.wait, clock=fc.clock, **kwargs)
    fc.warmup = warmup
    warmup.start()
    assert warmup.wait(5)
    return warmup, fc


def test_warmup_retries_connection_failures_then_ok():
    refused = ProviderUnavailable("無法連線 http://ollama:11434：Connection refused")
    emb = ScriptedEmbedder(
        refused, EnrichTimeout("請求逾時"), ConnectionRefusedError("refused")
    )
    warmup, fc = _run(emb)
    status = warmup.status()
    assert status["status"] == "ok"
    assert status["error"] is None
    assert status["attempts"] == 4
    assert emb.calls == 4
    assert fc.waits == [5.0, 10.0, 20.0]
    # 重試期間回報 retrying，附最近一次錯誤與嘗試次數
    first = fc.statuses[0]
    assert first["status"] == "retrying"
    assert first["attempts"] == 1
    assert "Connection refused" in first["error"]


def test_warmup_gives_up_after_max_total():
    emb = ScriptedEmbedder(*[ProviderUnavailable("Connection refused")] * 100)
    warmup, fc = _run(emb)
    status = warmup.status()
    assert status["status"] == "failed"
    assert "ProviderUnavailable" in status["error"]
    # 5+10+20+40+60*8=555 秒；再等 60 會超過 600 秒上限
    assert fc.waits == [5.0, 10.0, 20.0, 40.0] + [60.0] * 8
    assert status["attempts"] == 13
    assert sum(fc.waits) <= 600.0


def test_warmup_non_connection_error_is_not_retried():
    warmup, fc = _run(ScriptedEmbedder(vector=()))
    status = warmup.status()
    assert status["status"] == "failed"
    assert "空向量" in status["error"]
    assert status["attempts"] == 1
    assert fc.waits == []


def test_warmup_stop_interrupts_retry():
    emb = ScriptedEmbedder(*[ProviderUnavailable("Connection refused")] * 100)
    warmup = EmbeddingWarmup(emb, initial_delay=30.0)
    warmup.start()
    for _ in range(100):
        if warmup.status()["status"] == "retrying":
            break
        threading.Event().wait(0.01)
    warmup.stop(join_timeout=5)
    assert warmup.wait(1)
    status = warmup.status()
    assert status["status"] == "failed"
    assert "服務停止" in status["error"]
    assert emb.calls == 1
