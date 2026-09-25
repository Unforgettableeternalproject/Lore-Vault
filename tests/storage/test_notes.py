"""Note 儲存原語：讀寫、分頁、條件更新（樂觀鎖原語）。"""

from __future__ import annotations

import pytest

from lore_vault.schema import Note, SchemaError
from lore_vault.storage import notes
from lore_vault.storage.errors import DuplicateRecord, NotFound, UnknownVault

V = "folder/notes"


@pytest.fixture
def vault(add_vault):
    return add_vault(V)


def test_round_trip_normalizes_timestamps(conn, vault):
    note = Note(
        id="n-1",
        vault="Folder/Notes",
        title="標題",
        body="正文",
        topics=("a", "b"),
        links=("n-0",),
        created="2026-09-01T00:00:00+00:00",
        updated="2026-09-01T00:00:00.123456789Z",
    )
    stored = notes.insert_note(conn, "FOLDER/notes", note)
    assert stored.vault == V
    assert stored.created == "2026-09-01T00:00:00.000Z"
    assert stored.updated == "2026-09-01T00:00:00.123Z"
    assert notes.get_note(conn, V, "n-1") == stored


def test_duplicate_id_and_unknown_vault(conn, vault, add_note):
    add_note(V, "n-1", "t")
    with pytest.raises(DuplicateRecord):
        add_note(V, "n-1", "t")
    with pytest.raises(UnknownVault):
        add_note("folder/nope", "n-2", "t")


def test_get_notes_keeps_order_and_skips_missing(conn, vault, add_note):
    for i in range(3):
        add_note(V, f"n-{i}", "t")
    got = notes.get_notes(conn, V, ["n-2", "missing", "n-0"])
    assert [n.id for n in got] == ["n-2", "n-0"]
    with pytest.raises(TypeError):
        notes.get_notes(conn, V, "n-1")
    with pytest.raises(NotFound):
        notes.get_note(conn, V, "missing")


def test_list_notes_paginates_newest_first(conn, vault, add_note):
    for i in range(5):
        add_note(V, f"n-{i}", "t", ts=f"2026-09-0{i + 1}T00:00:00.000Z")
    page, cursor = notes.list_notes(conn, V, limit=2)
    assert [n.id for n in page] == ["n-4", "n-3"]
    page, cursor = notes.list_notes(conn, V, limit=2, cursor=cursor)
    assert [n.id for n in page] == ["n-2", "n-1"]
    page, cursor = notes.list_notes(conn, V, limit=2, cursor=cursor)
    assert [n.id for n in page] == ["n-0"] and cursor is None
    page, _ = notes.list_notes(conn, V, since="2026-09-04T00:00:00Z")
    assert [n.id for n in page] == ["n-4", "n-3"]


def test_conditional_update_succeeds_with_exact_version(conn, vault, add_note):
    note = add_note(V, "n-1", "舊標題")
    new = notes.update_note_if(conn, V, "n-1", note.updated, {"title": "新標題"})
    assert new is not None and new.title == "新標題"
    assert new.updated > note.updated
    assert notes.get_note(conn, V, "n-1") == new


def test_conditional_update_rejects_stale_version(conn, vault, add_note):
    note = add_note(V, "n-1", "原始")
    first = notes.update_note_if(conn, V, "n-1", note.updated, {"title": "A"})
    assert first is not None
    # 第二個寫入者拿的是舊版本：不寫入、回 None
    assert notes.update_note_if(conn, V, "n-1", note.updated, {"title": "B"}) is None
    assert notes.get_note(conn, V, "n-1").title == "A"


def test_version_compare_is_exact_string(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    # 同一時刻的另一種寫法也不算相符：比對的是資料庫裡的字串
    same_instant = note.updated.replace("Z", "+00:00")
    assert notes.update_note_if(conn, V, "n-1", same_instant, {"title": "x"}) is None


def test_update_in_same_millisecond_still_bumps_version(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    new = notes.update_note_if(
        conn, V, "n-1", note.updated, {"title": "x"}, now=note.updated
    )
    assert new is not None
    assert new.updated == "2026-09-01T00:00:00.001Z"
    # 時鐘回撥也一樣嚴格遞增
    newer = notes.update_note_if(
        conn, V, "n-1", new.updated, {"title": "y"}, now="2020-01-01T00:00:00Z"
    )
    assert newer is not None and newer.updated == "2026-09-01T00:00:00.002Z"


def test_update_validates_fields(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    with pytest.raises(SchemaError):
        notes.update_note_if(conn, V, "n-1", note.updated, {"vault": "folder/x"})
    with pytest.raises(SchemaError):
        notes.update_note_if(conn, V, "n-1", note.updated, {"title": ""})
    with pytest.raises(NotFound):
        notes.update_note_if(conn, V, "missing", note.updated, {"title": "x"})
    assert notes.get_note(conn, V, "n-1") == note
