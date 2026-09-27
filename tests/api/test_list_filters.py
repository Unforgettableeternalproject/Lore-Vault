"""`/v1/list` 的篩選（UI 筆記／文件頁）：title／author／author_state（note）、
title／statuses／extensions（文件）。篩選在 SQL 內、LIMIT 之前：offset 分頁不漏不重、
total 與 items 同一組條件；LIKE 萬用字元照字面比對；kind 專屬篩選會排除另一種；
參數不合法回 400。"""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import create_vault, write_note
from .test_documents_http import _config, uploaded
from .test_page_offset import _set_updated, _stamp

V = "folder/filter-a"


def _post(client, **body) -> dict:
    resp = client.post("/v1/list", json={"vault": V, "limit": 50, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(page: dict) -> list[str]:
    return [i["id"] for i in page["items"]]


@pytest.fixture
def notes(client, db_path):
    """六則 note（由新到舊 a～f），標題與作者各有差異；含 LIKE 萬用字元。"""
    create_vault(client, V)
    spec = [
        ("a", "注入預算 100%", "Minka"),
        ("b", "注入_預算", "minka"),
        ("c", "hook 路徑", "Xavier (Bernie)"),
        ("d", "注入預算草稿", None),
        ("e", "Budget notes", "Codex"),
        ("f", "無關", None),
    ]
    ids: dict[str, str] = {}
    for key, title, author in spec:
        extra = {"author": author} if author is not None else {}
        ids[key] = write_note(client, V, title, "body", **extra)["id"]
    order = list(ids)
    _set_updated(
        db_path,
        "notes",
        {ids[k]: _stamp(20 - i) for i, k in enumerate(order)},
    )
    return ids


def _notes(client, **body) -> dict:
    return _post(client, kinds=["note"], with_total=True, **body)


def test_title_is_case_insensitive_substring(client, notes):
    page = _notes(client, title="注入")
    assert _ids(page) == [notes["a"], notes["b"], notes["d"]]
    assert page["total"] == 3
    assert _ids(_notes(client, title="BUDGET")) == [notes["e"]]


def test_title_like_wildcards_are_literal(client, notes):
    # `%` 與 `_` 不當萬用字元：只配到字面含有的那則
    assert _ids(_notes(client, title="100%")) == [notes["a"]]
    assert _ids(_notes(client, title="_")) == [notes["b"]]
    assert _notes(client, title="%")["total"] == 1


def test_author_substring_and_state(client, notes):
    page = _notes(client, author="minka")
    assert _ids(page) == [notes["a"], notes["b"]] and page["total"] == 2
    assert _ids(_notes(client, author="(Bernie)")) == [notes["c"]]
    # 未具名：沒填 author（服務拒收空白名稱，只會是 null）
    missing = _notes(client, author_state="missing")
    assert set(_ids(missing)) == {notes["d"], notes["f"]}
    assert missing["total"] == 2
    named = _notes(client, author_state="named")
    assert named["total"] == 4 and notes["e"] in _ids(named)


def test_filters_combine_and_page_with_offset(client, notes):
    """title＋author 的交集，offset 分頁與 total 一致。"""
    seen: list[str] = []
    for offset in (0, 1, 2):
        page = _notes(
            client, title="注入", author_state="named", limit=1, offset=offset
        )
        assert page["total"] == 2
        seen.extend(_ids(page))
    assert seen == [notes["a"], notes["b"]]


def test_note_only_filters_exclude_documents(make_client, tmp_path, db_path):
    client = make_client(config=_config(tmp_path / "blobs"), document_worker=False)
    create_vault(client, V)
    note = write_note(client, V, "設計 notes", "body", author="Minka")["id"]
    doc = uploaded(client, b"# notes\n\nx\n", "design-notes.md", vault=V)["document_id"]
    # title 兩種都吃
    both = _post(client, title="notes", with_total=True)
    assert set(_ids(both)) == {note, doc} and both["total"] == 2
    # author 只屬 note：文件不列、total 也不算
    only_notes = _post(client, title="notes", author="minka", with_total=True)
    assert _ids(only_notes) == [note] and only_notes["total"] == 1


@pytest.fixture
def docs(make_client, tmp_path, db_path):
    client = make_client(config=_config(tmp_path / "blobs"), document_worker=False)
    create_vault(client, V)
    files = [
        ("r", "Report.PDF", b"%PDF-1.4 fake"),
        ("m", "readme.md", b"# readme\n"),
        ("k", "notes.markdown", b"# notes\n"),
        ("t", "todo_list.txt", b"todo\n"),
        ("y", "conf.yml", b"a: 1\n"),
    ]
    ids = {
        key: uploaded(client, data, name, vault=V)["document_id"]
        for key, name, data in files
    }
    _set_updated(
        db_path, "documents", {ids[k]: _stamp(20 - i) for i, k in enumerate(ids)}
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE documents SET status = 'ready' WHERE id IN (?, ?)",
            (ids["m"], ids["k"]),
        )
        conn.execute(
            "UPDATE documents SET status = 'failed', error_code = 'empty_extraction' "
            "WHERE id = ?",
            (ids["r"],),
        )
        conn.execute(
            "UPDATE documents SET status = 'extracting' WHERE id = ?", (ids["t"],)
        )
        conn.commit()
    finally:
        conn.close()
    # y 維持 pending
    return client, ids


def _docs(client, **body) -> dict:
    return _post(client, kinds=["document"], with_total=True, **body)


def test_document_status_filter(docs):
    client, ids = docs
    failed = _docs(client, statuses=["failed"])
    assert _ids(failed) == [ids["r"]] and failed["total"] == 1
    processing = _docs(client, statuses=["pending", "extracting"])
    assert _ids(processing) == [ids["t"], ids["y"]] and processing["total"] == 2


def test_document_extension_filter_uses_filename_suffix(docs):
    client, ids = docs
    # 不分大小寫、可多個（UI 把 md 展開成 md＋markdown）
    assert _ids(_docs(client, extensions=["pdf"])) == [ids["r"]]
    md = _docs(client, extensions=["md", "markdown"])
    assert _ids(md) == [ids["m"], ids["k"]] and md["total"] == 2
    assert _ids(_docs(client, extensions=[".YML"])) == [ids["y"]]


def test_document_filename_filter_and_offset(docs):
    client, ids = docs
    assert _ids(_docs(client, title="_list")) == [ids["t"]]
    assert _ids(_docs(client, title="report")) == [ids["r"]]
    seen: list[str] = []
    for offset in (0, 1):
        page = _docs(client, statuses=["ready"], limit=1, offset=offset)
        assert page["total"] == 2
        seen.extend(_ids(page))
    assert seen == [ids["m"], ids["k"]]


def test_document_only_filters_exclude_notes(docs):
    client, ids = docs
    write_note(client, V, "readme 筆記", "body")
    page = _post(client, title="readme", statuses=["ready"], with_total=True)
    assert _ids(page) == [ids["m"]] and page["total"] == 1
    # note 專屬＋文件專屬同時指定：沒有符合的項目
    empty = _post(client, author_state="missing", statuses=["ready"], with_total=True)
    assert empty["items"] == [] and empty["total"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"author": ""},
        {"author_state": "anonymous"},
        {"statuses": []},
        {"statuses": ["done"]},
        {"extensions": []},
        {"extensions": ["p.df"]},
        {"extensions": ["%"]},
    ],
)
def test_invalid_filters_are_rejected(client, body):
    create_vault(client, V)
    resp = client.post("/v1/list", json={"vault": V, **body})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"


def test_invalid_filter_rejected_even_when_kind_not_listed(client):
    """文件專屬的參數不合法，只列 note 時也要擋，不默默忽略。"""
    create_vault(client, V)
    resp = client.post(
        "/v1/list", json={"vault": V, "kinds": ["note"], "statuses": ["x"]}
    )
    assert resp.status_code == 400
