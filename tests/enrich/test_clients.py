"""Embedder／Summarizer 以 fake HTTP 驗證：成功、空字串、截斷、逾時、429、認證錯誤。"""

from __future__ import annotations

import urllib.error

import pytest

from lore_vault.config import EmbeddingConfig, Secret, SummaryConfig
from lore_vault.enrich import (
    SUMMARY_SYSTEM_PROMPT,
    EnrichError,
    EnrichTimeout,
    InvalidOutput,
    OllamaEmbedder,
    OpenAISummarizer,
    ProviderUnavailable,
    RateLimited,
    clients,
)

KEY = "sk-test-FAKEKEY-0123456789"


def summarizer(transport, **overrides):
    config = SummaryConfig(**overrides)
    return OpenAISummarizer(config, Secret(KEY), transport=transport)


def test_summary_success_sends_effort_and_token_limit(http):
    fake = http.Transport(http.chat("  結論：採用 SQLite，延遲 3ms。 \n"))
    result = summarizer(fake).summarize("標題", "正文")
    assert result == "結論：採用 SQLite，延遲 3ms。"
    req = fake.requests[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["body"]["model"] == "gpt-6-luna"
    assert req["body"]["reasoning_effort"] == "low"
    assert req["body"]["max_completion_tokens"] == SummaryConfig().max_completion_tokens
    assert req["body"]["messages"][0]["content"] == SUMMARY_SYSTEM_PROMPT
    assert "正文" in req["body"]["messages"][1]["content"]
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["timeout"] == SummaryConfig().timeout


def test_prompt_asks_for_zh_tw_conclusion_only():
    for phrase in ("繁體中文", "1 到 2 句", "結論", "關鍵數據", "不加評價"):
        assert phrase in SUMMARY_SYSTEM_PROMPT


@pytest.mark.parametrize("content", ["", "   \n\t", None])
def test_empty_or_blank_summary_is_failure(http, content):
    """gpt-6-luna 陷阱：推理吃光 token 時回空字串且 finish_reason 仍可能是 stop。"""
    fake = http.Transport(http.chat(content))
    with pytest.raises(InvalidOutput, match="空"):
        summarizer(fake).summarize("t", "b")


def test_length_truncation_is_failure_even_with_text(http):
    fake = http.Transport(http.chat("截斷到一半的摘", finish="length"))
    with pytest.raises(InvalidOutput, match="length"):
        summarizer(fake).summarize("t", "b")


def test_unexpected_finish_reason_is_failure(http):
    fake = http.Transport(http.chat("x", finish="content_filter"))
    with pytest.raises(InvalidOutput, match="content_filter"):
        summarizer(fake).summarize("t", "b")


def test_malformed_response_is_failure(http):
    fake = http.Transport(http.error(200, "not json"))
    with pytest.raises(InvalidOutput):
        summarizer(fake).summarize("t", "b")
    fake = http.Transport(http.error(200, '{"choices": []}'))
    with pytest.raises(InvalidOutput):
        summarizer(fake).summarize("t", "b")


def test_429_is_rate_limited_with_retry_after(http):
    fake = http.Transport(http.error(429, "slow down", {"Retry-After": "7"}))
    with pytest.raises(RateLimited) as info:
        summarizer(fake).summarize("t", "b")
    assert info.value.retry_after == 7.0


def test_auth_error_is_provider_unavailable_and_scrubs_key(http):
    # 就算服務把 key 回顯在錯誤內容裡，例外訊息也不能帶出來
    fake = http.Transport(http.error(401, f"Incorrect API key provided: {KEY}"))
    with pytest.raises(ProviderUnavailable) as info:
        summarizer(fake).summarize("t", "b")
    assert KEY not in str(info.value)
    assert "401" in str(info.value)


def test_server_error_is_retryable_failure(http):
    fake = http.Transport(http.error(503, "overloaded"))
    with pytest.raises(EnrichError) as info:
        summarizer(fake).summarize("t", "b")
    assert not isinstance(info.value, ProviderUnavailable)


def test_timeout_propagates(http):
    fake = http.Transport(EnrichTimeout("請求逾時"))
    with pytest.raises(EnrichTimeout):
        summarizer(fake).summarize("t", "b")


def test_summarizer_repr_and_missing_key():
    s = summarizer(lambda *a: None)
    assert KEY not in repr(s) and KEY not in repr(vars(s))
    with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
        OpenAISummarizer(SummaryConfig(), Secret(""))


def test_embed_success_and_payload(http):
    fake = http.Transport(http.embed([0.1, 0.2, 0.3, 0.4]))
    embedder = OllamaEmbedder(EmbeddingConfig(dim=4), transport=fake)
    assert embedder.embed("文字") == [0.1, 0.2, 0.3, 0.4]
    req = fake.requests[0]
    assert req["url"] == "http://localhost:11434/api/embed"
    assert req["body"] == {"model": "bge-m3", "input": "文字", "keep_alive": "30m"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10m", "10m"), (" 3600 ", 3600), ("-1", -1), ("0", 0)],
)
def test_embed_keep_alive_values(http, raw, expected):
    fake = http.Transport(http.embed([0.1, 0.2, 0.3, 0.4]))
    OllamaEmbedder(EmbeddingConfig(dim=4, keep_alive=raw), transport=fake).embed("x")
    assert fake.requests[0]["body"]["keep_alive"] == expected


def test_embed_empty_keep_alive_is_omitted(http):
    fake = http.Transport(http.embed([0.1, 0.2, 0.3, 0.4]))
    OllamaEmbedder(EmbeddingConfig(dim=4, keep_alive=""), transport=fake).embed("x")
    assert "keep_alive" not in fake.requests[0]["body"]


def test_embed_dimension_mismatch_is_failure(http):
    fake = http.Transport(http.embed([0.1, 0.2]))
    with pytest.raises(InvalidOutput, match="維度"):
        OllamaEmbedder(EmbeddingConfig(dim=4), transport=fake).embed("x")


def test_embed_missing_model_is_provider_unavailable(http):
    fake = http.Transport(http.error(404, '{"error":"model not found"}'))
    with pytest.raises(ProviderUnavailable):
        OllamaEmbedder(EmbeddingConfig(dim=4), transport=fake).embed("x")


# ── 預設 urllib 傳輸的例外轉換（monkeypatch urlopen，不打網路）──


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))],
)
def test_urllib_timeout_becomes_enrich_timeout(monkeypatch, exc):
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(clients.urllib.request, "urlopen", boom)
    with pytest.raises(EnrichTimeout):
        clients.urllib_transport("http://127.0.0.1:9/x", b"{}", {}, 0.1)


def test_urllib_connection_refused_is_provider_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(clients.urllib.request, "urlopen", boom)
    with pytest.raises(ProviderUnavailable):
        clients.urllib_transport("http://127.0.0.1:9/x", b"{}", {}, 0.1)
