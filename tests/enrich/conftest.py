"""補算測試共用：獨立資料庫、fake HTTP 傳輸、可控時鐘。不打任何網路。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from lore_vault.enrich import HttpResponse
from lore_vault.schema import Note, Vault
from lore_vault.storage.db import connect
from lore_vault.storage.notes import insert_note
from lore_vault.storage.vaults import upsert_vault

TS = "2026-09-01T00:00:00.000Z"
VAULT = "folder/enrich"


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lore.db")
    upsert_vault(connection, Vault(key=VAULT, display=VAULT, kind="repo"))
    yield connection
    connection.close()


@pytest.fixture
def add_note(conn):
    def add(note_id: str, title: str = "標題", body: str = "正文", **kw) -> Note:
        ts = kw.pop("ts", TS)
        note = Note(
            id=note_id,
            vault=VAULT,
            title=title,
            body=body,
            created=ts,
            updated=ts,
            **kw,
        )
        return insert_note(conn, VAULT, note, space="dev")

    return add


class Clock:
    """可控 UTC 牆鐘。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start or datetime(2026, 9, 2, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


class FakeTransport:
    """依序回傳預先排好的回應（例外則拋出、callable 則呼叫），並記下每次請求。"""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append(
            {
                "url": url,
                "body": json.loads(body),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("FakeTransport 沒有更多回應")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


def chat(content, finish="stop", status=200, headers=None) -> HttpResponse:
    body = {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ]
    }
    return HttpResponse(status, json.dumps(body).encode(), headers or {})


def embed(vector) -> HttpResponse:
    return HttpResponse(200, json.dumps({"embeddings": [vector]}).encode())


def error(status: int, text: str = "", headers=None) -> HttpResponse:
    return HttpResponse(status, text.encode(), headers or {})


@pytest.fixture
def http():
    """測試檔無法直接 import conftest（importlib 模式），經 fixture 提供 fake。"""
    from types import SimpleNamespace

    return SimpleNamespace(Transport=FakeTransport, chat=chat, embed=embed, error=error)
