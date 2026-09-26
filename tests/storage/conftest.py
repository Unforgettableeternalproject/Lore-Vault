"""儲存層測試共用 fixture：每個測試一個獨立的資料庫檔，結束時關閉連線。

Windows 上 `-wal`／`-shm` 檔在連線未關閉時刪不掉，所以一律經 fixture 開關。
"""

from __future__ import annotations

import pytest

from lore_vault.schema import Note, Vault
from lore_vault.storage.db import connect
from lore_vault.storage.notes import insert_note
from lore_vault.storage.vaults import upsert_vault

TS = "2026-09-01T00:00:00.000Z"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "lore.db"


@pytest.fixture
def conn(db_path):
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def add_vault(conn):
    def add(key: str, *, aliases: tuple[str, ...] = (), kind: str = "repo") -> str:
        vault = Vault(key=key, display=key, kind=kind, aliases=aliases)
        upsert_vault(conn, vault)
        return vault.key

    return add


@pytest.fixture
def add_note(conn):
    def add(
        vault: str,
        note_id: str,
        title: str,
        body: str = "",
        *,
        summary: str | None = None,
        topics: tuple[str, ...] = (),
        ts: str = TS,
    ) -> Note:
        note = Note(
            id=note_id,
            vault=vault,
            title=title,
            body=body,
            summary=summary,
            topics=topics,
            created=ts,
            updated=ts,
        )
        return insert_note(conn, vault, note, space="dev")

    return add


def episode_data(**overrides) -> dict:
    data = {
        "prompt_id": "p-1",
        "turn_index": 0,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "machine": "desktop-a",
        "started_at": "2026-09-01T02:00:00.000Z",
        "ended_at": "2026-09-01T02:05:00.000Z",
        "cwd": ["C:/repo"],
        "repo": "Repo",
        "repo_root": "C:/repo",
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "u",
        "assistant_text": "a",
        "tool_sequence": [{"name": "Edit", "count": 1}],
        "tool_calls_total": 1,
        "mcp_tools": [],
        "skills": [],
        "files_edited": [],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
    }
    data.update(overrides)
    return data


@pytest.fixture
def make_episode():
    from lore_vault.schema import Episode

    def make(**overrides) -> Episode:
        return Episode.from_dict(episode_data(**overrides))

    return make
