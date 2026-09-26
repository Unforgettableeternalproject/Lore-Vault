"""頁碼分頁與日期區間（UI 的分頁元件）：`/v1/list` 與 `/v1/concept_query` 的
`offset`／`with_total`／`until`。跨頁不漏不重、total 與實際筆數一致、日期邊界含端點、
offset 與 cursor 不能並用、與既有 cursor 分頁結果一致。"""

from __future__ import annotations

import sqlite3

import pytest

from lore_vault.schema.models import Concept
from lore_vault.storage import records
from lore_vault.storage.db import connect

from .conftest import create_vault, write_note
from .test_documents_http import _config, uploaded

V = "folder/page-a"
N = 11


def _set_updated(db_path, table: str, stamps: dict[str, str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        # created 不可晚於 updated：有 created 欄的表一起改
        sets = "updated = ?, created = ?" if "created" in cols else "updated = ?"
        for id_, ts in stamps.items():
            args = (ts, ts, id_) if "created" in cols else (ts, id_)
            conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", args)
        conn.commit()
    finally:
        conn.close()


def _stamp(day: int, hour: int = 12) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:00:00.000Z"


@pytest.fixture
def notes(client, db_path):
    """11 則 note，updated 分別是 9/1～9/11 中午（由新到舊為 9/11 → 9/1）。"""
    create_vault(client, V)
    ids = [write_note(client, V, f"n{i}", "body")["id"] for i in range(N)]
    _set_updated(db_path, "notes", {id_: _stamp(i + 1) for i, id_ in enumerate(ids)})
    return list(reversed(ids))  # 由新到舊


def _list(client, **body) -> dict:
    resp = client.post("/v1/list", json={"vault": V, "kinds": ["note"], **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("size", [1, 3, 4, 11, 30])
def test_offset_pages_cover_all_without_gaps_or_duplicates(client, notes, size):
    seen: list[str] = []
    offset = 0
    while True:
        page = _list(client, limit=size, offset=offset, with_total=True)
        assert page["total"] == N
        assert page["offset"] == offset
        seen.extend(item["id"] for item in page["items"])
        offset += size
        if offset >= page["total"]:
            assert page["has_more"] is False
            break
        assert page["has_more"] is True
    assert seen == notes  # 順序與內容都一致：不漏、不重


def test_offset_matches_cursor_paging(client, notes):
    by_cursor: list[str] = []
    cursor = None
    while True:
        page = _list(client, limit=4, **({"cursor": cursor} if cursor else {}))
        by_cursor.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert by_cursor == notes


def test_offset_past_end_is_empty(client, notes):
    page = _list(client, limit=5, offset=50, with_total=True)
    assert page["items"] == [] and page["total"] == N and page["has_more"] is False


def test_total_only_when_requested(client, notes):
    assert "total" not in _list(client, limit=5)


def test_date_range_includes_both_endpoints(client, notes):
    # 9/3 中午～9/5 中午：兩端點剛好等於 updated，應含在內
    page = _list(client, limit=30, since=_stamp(3), until=_stamp(5), with_total=True)
    assert page["total"] == 3
    assert [i["id"] for i in page["items"]] == notes[6:9]  # 9/5、9/4、9/3
    # 往內縮 1 毫秒就排除端點
    inner = _list(
        client,
        limit=30,
        since="2026-09-03T12:00:00.001Z",
        until="2026-09-05T11:59:59.999Z",
        with_total=True,
    )
    assert [i["id"] for i in inner["items"]] == notes[7:8]
    assert inner["total"] == 1


def test_date_range_with_offset(client, notes):
    ids: list[str] = []
    for offset in (0, 2, 4):
        page = _list(
            client,
            limit=2,
            offset=offset,
            since=_stamp(2),
            until=_stamp(7),
            with_total=True,
        )
        assert page["total"] == 6
        ids.extend(i["id"] for i in page["items"])
    assert ids == notes[4:10]


def test_offset_and_cursor_are_exclusive(client, notes):
    first = _list(client, limit=3)
    resp = client.post(
        "/v1/list",
        json={
            "vault": V,
            "kinds": ["note"],
            "limit": 3,
            "offset": 3,
            "cursor": first["next_cursor"],
        },
    )
    assert resp.status_code == 400
    neg = client.post("/v1/list", json={"vault": V, "limit": 3, "offset": -1})
    assert neg.status_code in (400, 422)


def test_merged_note_and_document_offset_pages(make_client, tmp_path, db_path):
    """note 與文件合併列表時，offset 分頁仍不漏不重、total 為兩者之和。"""
    client = make_client(config=_config(tmp_path / "blobs"), document_worker=False)
    create_vault(client, V)
    note_ids = [write_note(client, V, f"n{i}", "body")["id"] for i in range(5)]
    doc_ids = [
        uploaded(client, f"# doc {i}\n\ncontent {i}\n".encode(), f"d{i}.md", vault=V)[
            "document_id"
        ]
        for i in range(4)
    ]
    # 交錯的時間：note 在奇數日、文件在偶數日
    _set_updated(
        db_path, "notes", {n: _stamp(2 * i + 1) for i, n in enumerate(note_ids)}
    )
    _set_updated(
        db_path, "documents", {d: _stamp(2 * i + 2) for i, d in enumerate(doc_ids)}
    )
    expected = [
        i["id"]
        for i in client.post("/v1/list", json={"vault": V, "limit": 50}).json()["items"]
    ]
    assert len(expected) == 9
    seen: list[str] = []
    for offset in range(0, 9, 2):
        page = client.post(
            "/v1/list",
            json={"vault": V, "limit": 2, "offset": offset, "with_total": True},
        ).json()
        assert page["total"] == 9
        seen.extend(i["id"] for i in page["items"])
    assert seen == expected
    # 合併列表的日期區間：9/2～9/5 → 文件 9/2、note 9/3、文件 9/4、note 9/5
    ranged = client.post(
        "/v1/list",
        json={
            "vault": V,
            "limit": 50,
            "since": _stamp(2),
            "until": _stamp(5),
            "with_total": True,
        },
    ).json()
    assert ranged["total"] == 4
    assert [i["id"] for i in ranged["items"]] == [
        note_ids[2],
        doc_ids[1],
        note_ids[1],
        doc_ids[0],
    ]


# ── concept_query ──


@pytest.fixture
def concepts(client, db_path):
    repo = "github.com/owner/page-repo"
    create_vault(client, repo)
    conn = connect(db_path)
    try:
        for i in range(7):
            records.upsert_concept(
                conn,
                repo,
                Concept(
                    id=f"pc-{i}",
                    statement=f"s{i}",
                    kind="project-fact",
                    scope="page-repo",
                ),
            )
    finally:
        conn.close()
    _set_updated(db_path, "concepts", {f"pc-{i}": _stamp(i + 1) for i in range(7)})
    return [f"pc-{i}" for i in reversed(range(7))]


def _cq(client, **body) -> dict:
    resp = client.post("/v1/concept_query", json={"space": "dev", "vault": "*", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_concept_offset_pages_and_total(client, concepts):
    seen: list[str] = []
    for offset in range(0, 7, 3):
        page = _cq(client, limit=3, offset=offset, with_total=True)
        assert page["total"] == 7
        seen.extend(c["id"] for c in page["items"])
    assert seen == concepts
    assert "total" not in _cq(client, limit=3)


def test_concept_date_range_inclusive(client, concepts):
    page = _cq(client, limit=50, since=_stamp(2), until=_stamp(4), with_total=True)
    assert [c["id"] for c in page["items"]] == ["pc-3", "pc-2", "pc-1"]
    assert page["total"] == 3


def test_concept_offset_and_cursor_are_exclusive(client, concepts):
    first = _cq(client, limit=2)
    resp = client.post(
        "/v1/concept_query",
        json={
            "space": "dev",
            "vault": "*",
            "limit": 2,
            "offset": 2,
            "cursor": first["next_cursor"],
        },
    )
    assert resp.status_code == 400
