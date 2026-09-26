"""T-23：寫入端時間戳統一為 `YYYY-MM-DDTHH:MM:SS.sssZ`。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from lore_vault.schema import Concept, Injection, SchemaError
from lore_vault.storage import records, vectors
from lore_vault.storage.timeutil import (
    format_utc,
    next_after,
    normalize_utc,
    utc_now,
)

CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_utc_now_is_canonical():
    assert CANONICAL.match(utc_now())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-01T02:00:00Z", "2026-09-01T02:00:00.000Z"),
        ("2026-09-01T02:00:00+00:00", "2026-09-01T02:00:00.000Z"),
        ("2026-09-01T02:00:00.5Z", "2026-09-01T02:00:00.500Z"),
        ("2026-09-01T02:00:00.123Z", "2026-09-01T02:00:00.123Z"),
        # SurrealDB 奈秒精度：截到毫秒
        ("2026-09-01T02:00:00.123456789Z", "2026-09-01T02:00:00.123Z"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_utc(raw) == expected


@pytest.mark.parametrize(
    "bad", ["2026-09-01T02:00:00", "2026-09-01T10:00:00+08:00", "not a time", ""]
)
def test_non_utc_or_naive_is_rejected(bad):
    with pytest.raises(SchemaError):
        normalize_utc(bad)


def test_format_converts_other_offsets_but_rejects_naive():
    taipei = datetime(2026, 9, 1, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    assert format_utc(taipei) == "2026-09-01T02:00:00.000Z"
    with pytest.raises(SchemaError):
        format_utc(datetime(2026, 9, 1))


def test_next_after_is_strictly_increasing():
    prev = "2026-09-01T00:00:00.999Z"
    assert next_after(prev, prev) == "2026-09-01T00:00:01.000Z"
    assert next_after(prev, "2026-09-02T00:00:00Z") == "2026-09-02T00:00:00.000Z"


def test_every_timestamp_written_by_storage_is_canonical(
    conn, add_vault, add_note, make_episode
):
    """掃過所有表的時間欄：不論輸入寫法，存下的都是同一種格式。"""
    v = add_vault("folder/ts")
    note = add_note(v, "n-1", "t", ts="2026-09-01T00:00:00+00:00")
    from lore_vault.storage.notes import update_note_if

    update_note_if(conn, v, "n-1", note.updated, {"title": "x"}, space="dev")
    vectors.set_embedding(conn, v, "n-1", [1, 0], space="dev", dim=2)
    records.insert_episode(
        conn, v, make_episode(started_at="2026-09-01T02:00:00+00:00")
    )
    records.upsert_concept(conn, v, Concept(id="c", statement="s", kind=None))
    records.insert_injection(
        conn, v, Injection(session_id="s", prompt_id="p", injected=[])
    )
    columns = {
        "vaults": ["created"],
        "notes": ["created", "updated"],
        "note_embeddings": ["updated"],
        "episodes": ["started_at", "ended_at", "recorded"],
        "concepts": ["updated"],
        "injections": ["recorded"],
    }
    seen = 0
    for table, cols in columns.items():
        for col in cols:
            for (value,) in conn.execute(f"SELECT {col} FROM {table}"):
                assert CANONICAL.match(value), (table, col, value)
                seen += 1
    assert seen == 9
