"""`POST /v1/ask`（D11）：檢索 → 片段 → 假問答模型 → 引用防呆。不打真 API。"""

from __future__ import annotations

import json

import pytest

from lore_vault.ask.client import Completion
from lore_vault.config import AskConfig, Config, EmbeddingConfig, Secret
from lore_vault.enrich import HttpResponse, InvalidOutput

from .conftest import DIM, RaisingEmbedder, create_vault, embed_all, write_note

A = "folder/ask-a"
B = "folder/ask-b"
LORE = "lore/ask-world"


class FakeAnswerer:
    """依序回傳預排的輸出（str＝content；例外則拋出），記下每次 prompt。"""

    model = "fake-luna"

    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> Completion:
        self.calls.append((system, user))
        if not self.outputs:
            raise AssertionError("FakeAnswerer 沒有更多輸出")
        item = self.outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Completion(item, "fake-luna-2026", {"prompt_tokens": 10})


def answer(status: str, *points: tuple[str, list[str]]) -> str:
    return json.dumps(
        {
            "status": status,
            "points": [{"claim": c, "note_ids": ids} for c, ids in points],
        }
    )


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return body


@pytest.fixture
def seeded(make_client, db_path):
    """兩個 vault 各有 note；回傳 (make, ids)。"""

    def make(answerer=None, **overrides):
        return make_client(answerer=answerer, **overrides)

    setup = make_client()
    create_vault(setup, A)
    create_vault(setup, B)
    ids = {
        "db": write_note(setup, A, "資料庫選型", "決定採用 SQLite WAL 與 FTS5。")["id"],
        "embed": write_note(setup, A, "embedding 模型", "沿用 Ollama bge-m3。")["id"],
        "other": write_note(setup, B, "SQLite 備份", "每日備份 SQLite 檔。")["id"],
    }
    embed_all(db_path)
    return make, ids


def test_answered_returns_points_sources_and_metadata(seeded):
    make, ids = seeded
    fake = FakeAnswerer(answer("answered", ("採用 SQLite WAL", [ids["db"]])))
    c = make(fake)
    resp = c.post("/v1/ask", json={"question": "SQLite 資料庫選型", "vault": A})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "answered"
    assert data["answer"]["points"] == [
        {"claim": "採用 SQLite WAL", "note_ids": [ids["db"]], "unsupported": False}
    ]
    assert data["dropped_citations"] == []
    assert data["status_downgraded"] is False
    source_ids = [s["id"] for s in data["sources"]]
    assert ids["db"] in source_ids
    # vault 範圍：B 的 note 不會進 A 的片段
    assert ids["other"] not in source_ids
    first = data["sources"][0]
    assert set(first) == {
        "id",
        "vault",
        "title",
        "updated",
        "score",
        "excerpt_truncated",
    }
    assert first["vault"] == A
    assert data["model"] == "fake-luna-2026"
    assert data["usage"] == {"prompt_tokens": 10}
    assert data["degraded"] is False
    assert data["kinds"] == ["note"]
    assert data["k"] == 10
    assert set(data["latency_ms"]) == {"retrieval", "generation", "total"}
    assert "get" in data["notice"]
    # prompt：只根據片段、每個片段帶 note id、updated、標題與正文
    system, user = fake.calls[0]
    assert "只能根據" in system and "insufficient" in system and "矛盾" in system
    assert f"note_id={ids['db']}" in user
    assert "updated=" in user
    assert "標題：資料庫選型" in user
    assert "決定採用 SQLite WAL 與 FTS5。" in user


def test_insufficient_passes_through(seeded):
    make, _ = seeded
    c = make(FakeAnswerer(answer("insufficient")))
    data = c.post("/v1/ask", json={"question": "SQLite", "vault": A}).json()
    assert data["status"] == "insufficient"
    assert data["answer"]["points"] == []
    assert data["status_downgraded"] is False


def test_citations_outside_snippets_are_dropped(seeded):
    make, ids = seeded
    fake = FakeAnswerer(
        answer(
            "answered",
            ("有效加無效", [ids["db"], "note-made-up", ids["other"]]),
            ("全部無效", ["note-made-up"]),
            ("沒有引用", []),
        )
    )
    c = make(fake)
    data = c.post("/v1/ask", json={"question": "SQLite 資料庫", "vault": A}).json()
    points = data["answer"]["points"]
    assert points[0] == {
        "claim": "有效加無效",
        "note_ids": [ids["db"]],
        "unsupported": False,
    }
    assert points[1]["note_ids"] == [] and points[1]["unsupported"] is True
    assert points[2]["unsupported"] is True
    # 另一個 vault 的真實 note 也不算：只認本次片段
    assert data["dropped_citations"] == [
        {"point": 0, "note_id": "note-made-up"},
        {"point": 0, "note_id": ids["other"]},
        {"point": 1, "note_id": "note-made-up"},
    ]
    assert data["status"] == "answered"


def test_answered_without_any_valid_citation_is_downgraded(seeded):
    make, _ = seeded
    c = make(FakeAnswerer(answer("answered", ("編的", ["nope"]))))
    data = c.post("/v1/ask", json={"question": "SQLite", "vault": A}).json()
    assert data["status"] == "insufficient"
    assert data["status_downgraded"] is True
    assert data["answer"]["points"][0]["unsupported"] is True


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "[]",
        json.dumps({"status": "maybe", "points": []}),
        json.dumps({"status": "answered"}),
        json.dumps({"status": "answered", "points": [{"claim": "", "note_ids": []}]}),
        json.dumps({"status": "answered", "points": [{"claim": "x", "note_ids": [1]}]}),
        InvalidOutput("回答為空字串"),
    ],
)
def test_invalid_output_is_explicit_error(seeded, output):
    make, _ = seeded
    c = make(FakeAnswerer(output))
    _error(
        c.post("/v1/ask", json={"question": "SQLite", "vault": A}),
        500,
        "ask_invalid_output",
    )


def _chat(content, finish="stop", status=200, headers=None):
    body = {
        "model": "gpt-6-luna-2026",
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }
    return HttpResponse(status, json.dumps(body).encode(), headers or {})


class Recorder:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": json.loads(body), "timeout": timeout})
        return self.responses.pop(0)


def _openai_client(seeded, *responses, **ask_overrides):
    make, ids = seeded
    transport = Recorder(*responses)
    c = make(
        openai_key=Secret("sk-test-ask-0123456789"),
        llm_transport=transport,
        config=Config(
            embedding=EmbeddingConfig(dim=DIM), ask=AskConfig(**ask_overrides)
        ),
    )
    return c, transport, ids


def test_openai_path_sends_effort_schema_and_maps_usage(seeded):
    make, ids = seeded
    c, transport, ids = _openai_client(
        seeded, _chat(answer("answered", ("SQLite", [ids["db"]])))
    )
    data = c.post("/v1/ask", json={"question": "SQLite 資料庫", "vault": A}).json()
    assert data["status"] == "answered"
    assert data["model"] == "gpt-6-luna-2026"
    assert data["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "reasoning_tokens": 5,
    }
    body = transport.requests[0]["body"]
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == AskConfig().max_completion_tokens
    assert body["response_format"]["json_schema"]["strict"] is True
    assert transport.requests[0]["timeout"] == AskConfig().timeout


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (_chat(""), 500, "ask_invalid_output"),
        (_chat("   "), 500, "ask_invalid_output"),
        (_chat('{"status":', finish="length"), 500, "ask_invalid_output"),
        (
            HttpResponse(429, b"slow down", {"Retry-After": "7"}),
            429,
            "ask_rate_limited",
        ),
        (
            HttpResponse(401, b"bad key sk-test-ask-0123456789"),
            500,
            "ask_provider_error",
        ),
        (HttpResponse(500, b"boom"), 500, "ask_provider_error"),
    ],
)
def test_openai_failures_are_not_fake_success(seeded, response, status, code):
    c, _, _ = _openai_client(seeded, response)
    body = _error(
        c.post("/v1/ask", json={"question": "SQLite 資料庫", "vault": A}), status, code
    )
    assert "sk-test-ask-0123456789" not in json.dumps(body, ensure_ascii=False)
    if code == "ask_rate_limited":
        assert body["error"]["retry_after"] == 7.0


def test_snippet_max_chars_clips_body(seeded, db_path):
    make, _ = seeded
    setup = make()
    long_id = write_note(setup, A, "長篇 SQLite", "SQLite " + "很長的正文" * 200)["id"]
    embed_all(db_path)
    fake = FakeAnswerer(answer("answered", ("x", [long_id])))
    c = make(
        fake,
        config=Config(
            embedding=EmbeddingConfig(dim=DIM), ask=AskConfig(snippet_max_chars=50)
        ),
    )
    data = c.post("/v1/ask", json={"question": "長篇 SQLite", "vault": A}).json()
    source = next(s for s in data["sources"] if s["id"] == long_id)
    assert source["excerpt_truncated"] is True
    user = fake.calls[0][1]
    assert "很長的正文" * 20 not in user
    assert "正文過長，已截斷" in user


def test_missing_key_is_not_configured_error(seeded):
    make, _ = seeded
    c = make()  # 沒有 answerer、沒有 openai_key
    _error(
        c.post("/v1/ask", json={"question": "SQLite", "vault": A}),
        500,
        "ask_not_configured",
    )


def test_no_snippets_skips_model(seeded):
    make, _ = seeded
    fake = FakeAnswerer()  # 被呼叫就會拋錯（500）
    c = make(fake)
    create_vault(c, "folder/empty")
    resp = c.post("/v1/ask", json={"question": "任何問題", "vault": "folder/empty"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "insufficient"
    assert body["sources"] == []
    assert body["answer"]["points"] == []
    assert body["model"] is None and body["usage"] is None
    assert body["latency_ms"]["generation"] is None
    assert fake.calls == []


def test_degraded_retrieval_is_flagged_but_still_answers(seeded):
    make, ids = seeded
    fake = FakeAnswerer(answer("answered", ("SQLite", [ids["db"]])))
    c = make(fake, query_embedder=RaisingEmbedder(TimeoutError("ollama slow")))
    data = c.post("/v1/ask", json={"question": "SQLite", "vault": A}).json()
    assert data["degraded"] is True
    assert data["degraded_reason"]
    assert data["degraded_detail"]
    assert data["status"] == "answered"


def test_k_limits_snippets_and_is_bounded(seeded):
    make, ids = seeded
    fake = FakeAnswerer(answer("insufficient"))
    c = make(fake)
    data = c.post(
        "/v1/ask", json={"question": "SQLite 模型", "vault": A, "k": 1}
    ).json()
    assert data["k"] == 1
    assert len(data["sources"]) == 1
    assert fake.calls[0][1].count("note_id=") == 1
    for bad in (0, 21):
        _error(
            c.post("/v1/ask", json={"question": "SQLite", "vault": A, "k": bad}),
            400,
            "invalid_request",
        )


def test_kinds_only_note_chunk_reported_unsupported(seeded):
    make, _ = seeded
    fake = FakeAnswerer(answer("insufficient"))
    c = make(fake)
    data = c.post(
        "/v1/ask", json={"question": "SQLite", "vault": A, "kinds": ["note", "chunk"]}
    ).json()
    assert data["kinds"] == ["note"]
    assert data["unsupported_kinds"] == ["chunk"]
    _error(
        c.post("/v1/ask", json={"question": "SQLite", "vault": A, "kinds": ["chunk"]}),
        400,
        "unsupported_kind",
    )
    _error(
        c.post(
            "/v1/ask", json={"question": "SQLite", "vault": A, "kinds": ["concept"]}
        ),
        400,
        "unsupported_kind",
    )


def test_scope_all_vaults_and_space_isolation(seeded, db_path):
    make, ids = seeded
    setup = make()
    create_vault(setup, LORE, space="lore")
    write_note(setup, LORE, "SQLite 在世界觀", "SQLite 魔法。", space="lore")
    embed_all(db_path)
    fake = FakeAnswerer(answer("insufficient"))
    c = make(fake)
    data = c.post("/v1/ask", json={"question": "SQLite", "vault": "*"}).json()
    vaults = {s["vault"] for s in data["sources"]}
    assert vaults <= {A, B}
    assert ids["other"] in [s["id"] for s in data["sources"]]


def test_request_validation(seeded):
    make, _ = seeded
    c = make(FakeAnswerer())
    _error(c.post("/v1/ask", json={"question": "SQLite"}), 400, "vault_required")
    _error(
        c.post("/v1/ask", json={"question": "  ", "vault": A}), 400, "invalid_request"
    )
    _error(
        c.post("/v1/ask", json={"question": "q", "vault": "folder/nope"}),
        404,
        "unknown_vault",
    )
    assert (
        c.post("/v1/ask", json={"question": "q", "vault": A, "query": "x"}).status_code
        == 422
    )


def _ask_check(client) -> dict:
    data = client.post("/v1/status", json={}).json()
    (check,) = [c for c in data["doctor"]["checks"] if c["name"] == "ask.provider"]
    return check


def test_doctor_ask_provider_warns_without_key(make_client):
    # 拿掉保護（沒有 key）→ 不是 ok
    assert _ask_check(make_client())["status"] == "warn"
    assert _ask_check(make_client(answerer=FakeAnswerer()))["status"] == "pass"
    assert (
        _ask_check(make_client(openai_key=Secret("sk-test-ask-0123456789")))["status"]
        == "pass"
    )
