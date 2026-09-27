"""問答模型用戶端（D11）以 fake HTTP 驗證：必送 effort／schema、空字串與截斷算失敗。"""

from __future__ import annotations

import json

import pytest

from lore_vault.ask.client import RESPONSE_FORMAT, OpenAIAnswerer
from lore_vault.config import AskConfig, Secret
from lore_vault.enrich import (
    EnrichError,
    InvalidOutput,
    ProviderUnavailable,
    RateLimited,
)

KEY = "sk-test-ASKKEY-0123456789"
OK = json.dumps({"status": "insufficient", "points": []})


def answerer(transport, **overrides):
    return OpenAIAnswerer(AskConfig(**overrides), Secret(KEY), transport=transport)


def test_success_sends_effort_schema_and_limits(http):
    fake = http.Transport(http.chat(OK))
    completion = answerer(fake).complete("系統", "使用者")
    assert completion.content == OK
    assert completion.model == "gpt-6-luna"  # 回應沒帶 model 時用設定值
    body = fake.requests[0]["body"]
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == AskConfig().max_completion_tokens
    assert body["response_format"] == RESPONSE_FORMAT
    assert body["messages"] == [
        {"role": "system", "content": "系統"},
        {"role": "user", "content": "使用者"},
    ]
    assert fake.requests[0]["timeout"] == AskConfig().timeout


@pytest.mark.parametrize("content", ["", "   \n", None])
def test_empty_output_is_failure(http, content):
    with pytest.raises(InvalidOutput, match="空字串"):
        answerer(http.Transport(http.chat(content))).complete("s", "u")


def test_length_truncation_and_odd_finish_are_failures(http):
    with pytest.raises(InvalidOutput, match="截斷"):
        answerer(http.Transport(http.chat(OK, finish="length"))).complete("s", "u")
    with pytest.raises(InvalidOutput, match="未正常結束"):
        answerer(http.Transport(http.chat(OK, finish="content_filter"))).complete(
            "s", "u"
        )


def test_http_errors_are_classified_and_key_scrubbed(http):
    with pytest.raises(RateLimited) as limited:
        answerer(
            http.Transport(http.error(429, "slow", {"Retry-After": "3"}))
        ).complete("s", "u")
    assert limited.value.retry_after == 3.0
    with pytest.raises(ProviderUnavailable) as auth:
        answerer(http.Transport(http.error(401, f"bad {KEY}"))).complete("s", "u")
    assert KEY not in str(auth.value)
    with pytest.raises(EnrichError):
        answerer(http.Transport(http.error(500, "boom"))).complete("s", "u")


def test_missing_key_and_repr():
    with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
        OpenAIAnswerer(AskConfig(), None)
    with pytest.raises(ProviderUnavailable):
        OpenAIAnswerer(AskConfig(), Secret("  "))
    assert KEY not in repr(OpenAIAnswerer(AskConfig(), Secret(KEY)))
