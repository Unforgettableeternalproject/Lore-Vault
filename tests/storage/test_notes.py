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
        principal="xavier",
        id="n-1",
        vault="Folder/Notes",
        title="標題",
        body="正文",
        topics=("a", "b"),
        links=("n-0",),
        created="2026-09-01T00:00:00+00:00",
        updated="2026-09-01T00:00:00.123456789Z",
    )
    stored = notes.insert_note(conn, "FOLDER/notes", note, space="dev")
    assert stored.vault == V
    assert stored.created == "2026-09-01T00:00:00.000Z"
    assert stored.updated == "2026-09-01T00:00:00.123Z"
    assert notes.get_note(conn, V, "n-1", space="dev") == stored


def test_duplicate_id_and_unknown_vault(conn, vault, add_note):
    add_note(V, "n-1", "t")
    with pytest.raises(DuplicateRecord):
        add_note(V, "n-1", "t")
    with pytest.raises(UnknownVault):
        add_note("folder/nope", "n-2", "t")


def test_insert_requires_principal(conn, vault):
    """A22：principal 是最底層必填（匯入等繞過服務層的路徑也擋），不預設成任何人。"""
    note = Note(
        id="n-x",
        vault=V,
        title="t",
        body="b",
        created="2026-09-01T00:00:00.000Z",
        updated="2026-09-01T00:00:00.000Z",
    )
    with pytest.raises(SchemaError, match="principal"):
        notes.insert_note(conn, V, note, space="dev")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_update_editor_sets_updated_by_without_touching_author(conn, vault, add_note):
    stored = add_note(V, "n-1", "t")
    first = notes.update_note_if(
        conn, V, "n-1", stored.updated, {"body": "x"}, space="dev", editor=("B", "p")
    )
    assert (first.author, first.updated_by, first.updated_by_principal) == (
        None,
        "B",
        "p",
    )
    # 不帶 editor 的內部呼叫：作者欄位不動
    second = notes.update_note_if(
        conn, V, "n-1", first.updated, {"body": "y"}, space="dev"
    )
    assert (second.updated_by, second.updated_by_principal) == ("B", "p")
    assert notes.get_note(conn, V, "n-1", space="dev").updated_by == "B"


def test_get_notes_keeps_order_and_skips_missing(conn, vault, add_note):
    for i in range(3):
        add_note(V, f"n-{i}", "t")
    got = notes.get_notes(conn, V, ["n-2", "missing", "n-0"], space="dev")
    assert [n.id for n in got] == ["n-2", "n-0"]
    with pytest.raises(TypeError):
        notes.get_notes(conn, V, "n-1", space="dev")
    with pytest.raises(NotFound):
        notes.get_note(conn, V, "missing", space="dev")


def test_list_notes_paginates_newest_first(conn, vault, add_note):
    for i in range(5):
        add_note(V, f"n-{i}", "t", ts=f"2026-09-0{i + 1}T00:00:00.000Z")
    page, cursor = notes.list_notes(conn, V, space="dev", limit=2)
    assert [n.id for n in page] == ["n-4", "n-3"]
    page, cursor = notes.list_notes(conn, V, space="dev", limit=2, cursor=cursor)
    assert [n.id for n in page] == ["n-2", "n-1"]
    page, cursor = notes.list_notes(conn, V, space="dev", limit=2, cursor=cursor)
    assert [n.id for n in page] == ["n-0"] and cursor is None
    page, _ = notes.list_notes(conn, V, space="dev", since="2026-09-04T00:00:00Z")
    assert [n.id for n in page] == ["n-4", "n-3"]


def test_conditional_update_succeeds_with_exact_version(conn, vault, add_note):
    note = add_note(V, "n-1", "舊標題")
    new = notes.update_note_if(
        conn, V, "n-1", note.updated, {"title": "新標題"}, space="dev"
    )
    assert new is not None and new.title == "新標題"
    assert new.updated > note.updated
    assert notes.get_note(conn, V, "n-1", space="dev") == new


def test_conditional_update_rejects_stale_version(conn, vault, add_note):
    note = add_note(V, "n-1", "原始")
    first = notes.update_note_if(
        conn, V, "n-1", note.updated, {"title": "A"}, space="dev"
    )
    assert first is not None
    # 第二個寫入者拿的是舊版本：不寫入、回 None
    assert (
        notes.update_note_if(conn, V, "n-1", note.updated, {"title": "B"}, space="dev")
        is None
    )
    assert notes.get_note(conn, V, "n-1", space="dev").title == "A"


def test_version_compare_is_exact_string(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    # 同一時刻的另一種寫法也不算相符：比對的是資料庫裡的字串
    same_instant = note.updated.replace("Z", "+00:00")
    assert (
        notes.update_note_if(conn, V, "n-1", same_instant, {"title": "x"}, space="dev")
        is None
    )


def test_update_in_same_millisecond_still_bumps_version(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    new = notes.update_note_if(
        conn, V, "n-1", note.updated, {"title": "x"}, space="dev", now=note.updated
    )
    assert new is not None
    assert new.updated == "2026-09-01T00:00:00.001Z"
    # 時鐘回撥也一樣嚴格遞增
    newer = notes.update_note_if(
        conn,
        V,
        "n-1",
        new.updated,
        {"title": "y"},
        space="dev",
        now="2020-01-01T00:00:00Z",
    )
    assert newer is not None and newer.updated == "2026-09-01T00:00:00.002Z"


def test_update_validates_fields(conn, vault, add_note):
    note = add_note(V, "n-1", "t")
    with pytest.raises(SchemaError):
        notes.update_note_if(
            conn, V, "n-1", note.updated, {"vault": "folder/x"}, space="dev"
        )
    with pytest.raises(SchemaError):
        notes.update_note_if(conn, V, "n-1", note.updated, {"title": ""}, space="dev")
    with pytest.raises(NotFound):
        notes.update_note_if(
            conn, V, "missing", note.updated, {"title": "x"}, space="dev"
        )
    assert notes.get_note(conn, V, "n-1", space="dev") == note
