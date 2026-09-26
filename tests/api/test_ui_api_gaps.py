"""UI 回報的 API 缺口：查重預覽（dry_run）、list 摘要與預算、note 的 superseded_by、
`[[標題]]` 連結解析、get 的 metadata 模式與 chunk overlap、UI 限制值、標籤清單、
plan_changed 附新 token。每項含成功／錯誤與 space／vault 範圍（不洩漏）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, UiConfig
from lore_vault.documents.worker import DocumentWorker
from lore_vault.schema import AUTHOR_MAX_CHARS
from lore_vault.storage import admin
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect

from .conftest import (
    DIM,
    UI_LOGIN,
    FakeEmbedder,
    create_vault,
    make_settings,
    seed_ui_account,
    write_note,
)
from .test_manage_http import _config, _upload, code, confirm, ok, post

DEV = "folder/manage-a"
DEV_B = "folder/gap-b"
LORE = "lore/world"
UI = {"X-Lore-Vault-UI": "1"}


@pytest.fixture
def doc_client(make_client, tmp_path):
    client = make_client(config=_config(tmp_path / "blobs"), document_worker=False)
    create_vault(client, DEV)
    return client


@pytest.fixture
def doc_worker(db_path, tmp_path):
    def run():
        conn = connect(db_path)
        try:
            DocumentWorker(
                conn,
                _config(tmp_path / "blobs"),
                blobs=BlobStore(tmp_path / "blobs"),
                embedder=FakeEmbedder(),
            ).run_once()
        finally:
            conn.close()

    return run


def _counts(db_path) -> tuple[int, int]:
    conn = connect(db_path)
    try:
        notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
        fts = conn.execute("SELECT count(*) FROM note_fts").fetchone()[0]
        return notes, fts
    finally:
        conn.close()


def _get_note(client, vault: str, note_id: str, space: str = "dev", **extra) -> dict:
    data = ok(post(client, "/v1/get", vault=vault, ids=[note_id], space=space, **extra))
    assert len(data["items"]) == 1, data
    return data["items"][0]


# ── 1. 查重預覽（write dry_run）──


def test_dry_run_reports_duplicates_without_writing(client, db_path):
    create_vault(client, DEV)
    first = write_note(client, DEV, "SQLite 選型", "決定用 SQLite WAL 與 FTS5")
    before = _counts(db_path)
    resp = post(
        client,
        "/v1/write",
        vault=DEV,
        title="SQLite 選型",
        body="決定用 SQLite WAL 與 FTS5",
        dry_run=True,
    )
    data = ok(resp, 200)
    assert data["dry_run"] is True
    assert "id" not in data and "updated" not in data
    assert data["vault"] == DEV
    assert [d["id"] for d in data["duplicates"]] == [first["id"]]
    assert "dedup_degraded" in data
    assert _counts(db_path) == before


def test_dry_run_same_scope_errors_as_write(client, db_path):
    create_vault(client, DEV)
    create_vault(client, LORE, space="lore")
    write_note(client, LORE, "世界觀", "魔法體系設定", space="lore")
    before = _counts(db_path)
    body = {"title": "世界觀", "body": "魔法體系設定", "dry_run": True}
    # lore 的 vault 在 dev 查不到（與不存在相同）
    resp = post(client, "/v1/write", vault=LORE, **body)
    assert resp.status_code == 404 and code(resp) == "unknown_vault"
    # dev 內不會看到 lore 的重複
    data = ok(post(client, "/v1/write", vault=DEV, **body), 200)
    assert data["duplicates"] == []
    # '*' 不可寫、supersedes 不存在、缺 space：與正式寫入相同的錯誤
    resp = post(client, "/v1/write", vault="*", **body)
    assert resp.status_code == 400 and code(resp) == "vault_required"
    resp = post(client, "/v1/write", vault=DEV, supersedes="nope", **body)
    assert resp.status_code == 404 and code(resp) == "not_found"
    resp = client.post("/v1/write", json={"vault": DEV, "space": "", **body})
    assert resp.status_code == 400 and code(resp) == "space_required"
    assert _counts(db_path) == before


# ── 2. list 的 summary／summary_source 與預算 ──


def test_list_note_items_carry_summary_with_lead_fallback(client):
    create_vault(client, DEV)
    write_note(client, DEV, "有正文", "# 標題行\n\n第一段內容\n\n第二段")
    write_note(client, DEV, "空正文", "# 只有標題")
    items = ok(post(client, "/v1/list", vault=DEV))["items"]
    by_title = {i["title"]: i for i in items}
    assert by_title["有正文"]["summary"] == "第一段內容"
    assert by_title["有正文"]["summary_source"] == "lead"
    assert by_title["空正文"]["summary"] is None
    assert by_title["空正文"]["summary_source"] == "none"


def test_list_summary_budget_omits_without_dropping_items(client):
    create_vault(client, DEV)
    for n in range(4):
        write_note(client, DEV, f"n{n}", "甲" * 100)
    data = ok(post(client, "/v1/list", vault=DEV, budget=150))
    items = data["items"]
    assert len(items) == 4  # 預算不影響項目與分頁
    # 公平分配：下限 40 給得起前 3 則，150 平分各 50（截短標記），尾端 1 則省略
    assert [i["summary"] for i in items[:3]] == ["甲" * 49 + "…"] * 3
    assert [i["summary_truncated"] for i in items] == [True, True, True, False]
    assert items[3]["summary_source"] == "omitted" and items[3]["summary"] is None
    assert data["truncated"] is True
    assert data["summaries_truncated"] == 3 and data["summaries_omitted"] == 1
    assert data["used_chars"] == 150 and data["budget"] == 150
    # 連第一則的下限都給不起：截斷它（至少給一則），其餘省略
    data = ok(post(client, "/v1/list", vault=DEV, budget=10))
    assert len(data["items"][0]["summary"]) == 10
    assert data["items"][0]["summary"].endswith("…")
    assert data["summaries_omitted"] == 3 and data["summaries_truncated"] == 1
    # 預算夠：不截
    data = ok(post(client, "/v1/list", vault=DEV))
    assert data["truncated"] is False and data["summaries_omitted"] == 0
    assert data["summaries_truncated"] == 0
    assert all(i["summary_truncated"] is False for i in data["items"])
    resp = post(client, "/v1/list", vault=DEV, budget=0)
    assert resp.status_code == 400 and code(resp) == "invalid_request"


# ── 3. note 的 superseded_by ──


def test_superseded_by_on_get_and_list(client):
    create_vault(client, DEV)
    old = write_note(client, DEV, "舊結論", "原本的決定")
    first = write_note(client, DEV, "更正一", "改了", supersedes=old["id"])
    second = write_note(client, DEV, "更正二", "又改了", supersedes=old["id"])
    got = _get_note(client, DEV, old["id"])
    # 多則指向同一則：取 updated 最新者
    assert got["superseded_by"] == second["id"]
    assert _get_note(client, DEV, first["id"])["superseded_by"] is None
    items = ok(post(client, "/v1/list", vault=DEV))["items"]
    by_id = {i["id"]: i for i in items}
    assert by_id[old["id"]]["superseded_by"] == second["id"]
    assert by_id[second["id"]]["supersedes"] == old["id"]


def test_superseded_by_only_same_vault(client, db_path):
    create_vault(client, DEV)
    create_vault(client, DEV_B)
    old = write_note(client, DEV, "舊結論", "原本的決定")
    # 別的 vault 不能指向它
    resp = post(
        client, "/v1/write", vault=DEV_B, title="t", body="b", supersedes=old["id"]
    )
    assert resp.status_code == 404 and code(resp) == "not_found"
    # 就算資料被直接改成跨 vault，反向查詢也只看同 vault
    other = write_note(client, DEV_B, "別處", "內容")
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE notes SET supersedes = ? WHERE id = ?", (old["id"], other["id"])
        )
        conn.commit()
    finally:
        conn.close()
    assert _get_note(client, DEV, old["id"])["superseded_by"] is None


# ── 4. `[[標題]]` 連結解析 ──


def test_write_resolves_wikilinks_and_merges_explicit(client):
    create_vault(client, DEV)
    target = write_note(client, DEV, "[Decision] 選 SQLite", "內容")
    alias = write_note(client, DEV, "資料模型", "內容")
    note = write_note(
        client,
        DEV,
        "引用者",
        "見 [[[Decision] 選 SQLite]]、[[資料模型|模型]]、[[資料模型#欄位]]、[[不存在]]",
        links=["manual-id"],
    )
    assert note["links"] == ["manual-id", target["id"], alias["id"]]
    assert note["unresolved_links"] == [
        {"target": "不存在", "status": "unresolved", "candidates": []}
    ]
    stored = _get_note(client, DEV, note["id"])
    assert stored["links"] == ["manual-id", target["id"], alias["id"]]


def test_ambiguous_and_cross_vault_links_are_not_written(client):
    create_vault(client, DEV)
    create_vault(client, DEV_B)
    create_vault(client, LORE, space="lore")
    a = write_note(client, DEV, "重複標題", "一")
    b = write_note(client, DEV, "重複 標題", "二")  # 壓空白、casefold 後不同 → 不歧義
    c = write_note(client, DEV, "重複標題", "三")
    write_note(client, DEV_B, "別的 vault", "內容")
    write_note(client, LORE, "世界觀", "內容", space="lore")
    note = write_note(client, DEV, "x", "[[重複標題]] [[別的 vault]] [[世界觀]]")
    assert note["links"] == []
    unresolved = {u["target"]: u for u in note["unresolved_links"]}
    assert unresolved["重複標題"]["status"] == "ambiguous"
    assert sorted(unresolved["重複標題"]["candidates"]) == sorted([a["id"], c["id"]])
    # 別的 vault、別的 space：當成不存在，候選不帶出任何 id
    for target in ("別的 vault", "世界觀"):
        assert unresolved[target] == {
            "target": target,
            "status": "unresolved",
            "candidates": [],
        }
    assert b["id"] not in note["links"]


def test_update_link_merge_rules(client):
    create_vault(client, DEV)
    t1 = write_note(client, DEV, "目標一", "內容")
    t2 = write_note(client, DEV, "目標二", "內容")
    note = write_note(client, DEV, "本體", "見 [[目標一]]", links=["manual"])
    assert note["links"] == ["manual", t1["id"]]

    def update(**changes) -> dict:
        current = _get_note(client, DEV, note["id"])
        return ok(
            post(
                client,
                "/v1/update",
                vault=DEV,
                id=note["id"],
                expected_updated=current["updated"],
                **changes,
            )
        )

    # 正文拿掉 [[目標一]]、改連 [[目標二]]：自動連結跟著換，明確加的保留
    data = update(body="改見 [[目標二]] 與 [[本體]] 與 [[沒有]]")
    assert data["links"] == ["manual", t2["id"]]  # 自己不連自己
    assert data["unresolved_links"] == [
        {"target": "沒有", "status": "unresolved", "candidates": []}
    ]
    # 只改 title：links 不動、不重新解析
    data = update(title="本體（改名）")
    assert data["links"] == ["manual", t2["id"]] and data["unresolved_links"] == []
    # 明傳 links：傳入值 ∪ 目前正文解析結果
    data = update(links=["other"])
    assert data["links"] == ["other", t2["id"]]
    assert _get_note(client, DEV, note["id"])["links"] == ["other", t2["id"]]


def test_dry_run_previews_links(client, db_path):
    create_vault(client, DEV)
    target = write_note(client, DEV, "目標", "內容")
    before = _counts(db_path)
    data = ok(
        post(
            client,
            "/v1/write",
            vault=DEV,
            title="新",
            body="[[目標]] [[缺]]",
            dry_run=True,
        ),
        200,
    )
    assert data["links"] == [target["id"]]
    assert [u["target"] for u in data["unresolved_links"]] == ["缺"]
    assert _counts(db_path) == before


# ── 5. get 的 metadata 模式 ──


def test_get_meta_returns_no_body_and_uses_no_budget(client):
    create_vault(client, DEV)
    note = write_note(client, DEV, "長文", "字" * 500)
    data = ok(post(client, "/v1/get", vault=DEV, ids=[note["id"]], fields="meta"))
    item = data["items"][0]
    assert "body" not in item
    assert item["body_chars"] == 500 and item["truncated"] is False
    assert item["summary_source"] == "lead" and item["title"] == "長文"
    assert data["used_chars"] == 0 and data["truncated"] is False
    resp = post(client, "/v1/get", vault=DEV, ids=[note["id"]], fields="body")
    assert resp.status_code == 400 and code(resp) == "invalid_request"
    # 範圍照舊：別的 space 讀不到
    create_vault(client, LORE, space="lore")
    data = ok(
        post(
            client,
            "/v1/get",
            vault="*",
            ids=[note["id"]],
            fields="meta",
            space="lore",
        )
    )
    assert data["items"] == [] and data["missing"] == [note["id"]]


# ── 5／6. 文件：逐段取文字、meta 字數一致、chunk overlap ──

LONG_DOC = (
    "# 第一章\n\n"
    + "".join(f"這是第{i}句設定，描述世界觀的細節與規則。" for i in range(400))
    + "\n\n# 第二章\n\n短短的結尾。\n"
)


def test_document_meta_chars_match_full_text_and_budget(doc_client, doc_worker):
    up = _upload(doc_client, "world.md", LONG_DOC.encode("utf-8"))
    doc_worker()
    doc_id = up["document_id"]
    full = ok(post(doc_client, "/v1/get", vault=DEV, ids=[doc_id], budget=1_000_000))
    text = full["items"][0]["text"]
    assert full["items"][0]["truncated"] is False
    meta = ok(post(doc_client, "/v1/get", vault=DEV, ids=[doc_id], fields="meta"))
    item = meta["items"][0]
    assert "text" not in item and item["truncated"] is False
    assert item["text_chars"] == len(text) == full["items"][0]["text_chars"]
    assert item["status"] == "ready" and meta["used_chars"] == 0
    cut = ok(post(doc_client, "/v1/get", vault=DEV, ids=[doc_id], budget=1234))
    assert cut["items"][0]["text"] == text[:1234]
    assert cut["items"][0]["truncated"] is True and cut["used_chars"] == 1234
    assert cut["items"][0]["text_chars"] == len(text)


def test_chunk_get_reports_overlap(doc_client, doc_worker):
    up = _upload(doc_client, "world.md", LONG_DOC.encode("utf-8"))
    doc_worker()
    doc = ok(
        post(doc_client, "/v1/get", vault=DEV, ids=[up["document_id"]], fields="meta")
    )
    count = doc["items"][0]["chunk_count"]
    assert count >= 3
    uuid = up["document_id"].removeprefix("doc:")
    ids = [f"chunk:{uuid}:{i}" for i in range(count)]
    data = ok(post(doc_client, "/v1/get", vault=DEV, ids=ids, budget=1_000_000))
    overlaps = [i["overlap"] for i in data["items"]]
    assert overlaps[0] == 0  # 第一段沒有前一段
    assert any(o > 0 for o in overlaps)  # 同段內切開的有重疊
    assert overlaps[-1] == 0  # 第二章是新段落
    # 去掉重疊後串起來 = 整份文件文字（段落之間空一行）
    rebuilt = ""
    for item in data["items"]:
        if item["overlap"]:
            rebuilt += item["text"][item["overlap"] :]
        else:
            rebuilt += ("\n\n" if rebuilt else "") + item["text"]
    full = ok(
        post(doc_client, "/v1/get", vault=DEV, ids=[up["document_id"]], budget=10**6)
    )
    assert rebuilt == full["items"][0]["text"]
    meta = ok(post(doc_client, "/v1/get", vault=DEV, ids=ids[:1], fields="meta"))
    assert "text" not in meta["items"][0] and meta["items"][0]["overlap"] == 0


# ── 7. UI 限制值 ──


def test_session_reports_limits(db_path, tmp_path):
    config = Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(max_file_bytes=12345, max_chars=678),
        ui=UiConfig(),
    )
    seed_ui_account(db_path)
    app = create_app(make_settings(db_path, config=config))
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/ui/api/session", headers=UI).status_code == 401
        resp = c.post("/ui/api/login", json=UI_LOGIN, headers=UI)
        assert resp.status_code == 204
        limits = c.get("/ui/api/session", headers=UI).json()["limits"]
    assert limits["max_file_bytes"] == 12345 and limits["max_chars"] == 678
    assert limits["author_max_chars"] == AUTHOR_MAX_CHARS
    assert limits["get_max_ids"] == 50 and limits["list_max_limit"] == 200
    assert limits["recall_max_limit"] == 100


# ── 8. 標籤清單 ──


def test_topics_counts_and_space_scope(client):
    create_vault(client, DEV)
    create_vault(client, DEV_B)
    create_vault(client, LORE, space="lore")
    write_note(client, DEV, "a", "x", topics=["db", "sqlite"])
    write_note(client, DEV, "b", "x", topics=["db"])
    write_note(client, DEV_B, "c", "x", topics=["ui"])
    write_note(client, LORE, "d", "x", topics=["魔法", "db"], space="lore")
    data = ok(post(client, "/v1/topics", space="dev", vault=DEV))
    assert data == {
        "space": "dev",
        "vault": DEV,
        "topics": [{"topic": "db", "count": 2}, {"topic": "sqlite", "count": 1}],
    }
    data = ok(post(client, "/v1/topics", space="dev", vault="*"))
    assert data["vault"] == "*"
    assert data["topics"] == [
        {"topic": "db", "count": 2},
        {"topic": "sqlite", "count": 1},
        {"topic": "ui", "count": 1},
    ]
    # lore 的 '*' 只看 lore（不含 dev 的 db 兩筆）
    data = ok(post(client, "/v1/topics", space="lore", vault="*"))
    assert data["topics"] == [
        {"topic": "db", "count": 1},
        {"topic": "魔法", "count": 1},
    ]
    # 別的 space 的 vault 與不存在相同
    resp = post(client, "/v1/topics", space="lore", vault=DEV)
    assert resp.status_code == 404 and code(resp) == "unknown_vault"
    assert "sqlite" not in resp.text
    resp = post(client, "/v1/topics", vault=DEV)
    assert resp.status_code == 400 and code(resp) == "space_required"
    resp = post(client, "/v1/topics", space="dev")
    assert resp.status_code == 400 and code(resp) == "vault_required"


# ── 9. plan_changed 附新 token ──


def test_plan_changed_returns_new_token_and_old_one_stays_refused(client):
    create_vault(client, DEV)
    write_note(client, DEV, "一", "內容")
    body = {"space": "dev", "key": DEV}
    planned = ok(post(client, "/v1/vault_delete", **body))
    write_note(client, DEV, "二", "規劃後新增")
    resp = post(
        client, "/v1/vault_delete", **body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 409 and code(resp) == "plan_changed"
    err = resp.json()["error"]
    assert err["plan"]["counts"]["notes"] == 2
    assert err["confirm_token"] and err["confirm_token"] != planned["confirm_token"]
    assert err["expires_at"].endswith("Z")
    # 沒有自動執行
    assert ok(post(client, "/v1/vault_resolve", key=DEV))["note_count"] == 2
    # 舊 token 仍被拒、仍不執行
    resp = post(
        client, "/v1/vault_delete", **body, confirm_token=planned["confirm_token"]
    )
    assert resp.status_code == 409 and code(resp) == "plan_changed"
    assert ok(post(client, "/v1/vault_resolve", key=DEV))["note_count"] == 2
    # 新 token：使用者確認後執行
    done = ok(
        post(client, "/v1/vault_delete", **body, confirm_token=err["confirm_token"])
    )
    assert done["executed"] is True and done["plan"]["counts"]["notes"] == 2
    resp = post(client, "/v1/vault_resolve", key=DEV)
    assert resp.status_code == 404


def test_plan_changed_new_token_is_bound_to_same_args(client):
    create_vault(client, DEV)
    note = write_note(client, DEV, "一", "內容")
    body = {"space": "dev", "vault": DEV, "id": note["id"]}
    planned = ok(post(client, "/v1/note_delete", **body))
    current = _get_note(client, DEV, note["id"])
    ok(
        post(
            client,
            "/v1/update",
            vault=DEV,
            id=note["id"],
            expected_updated=current["updated"],
            body="改過",
        )
    )
    resp = post(
        client, "/v1/note_delete", **body, confirm_token=planned["confirm_token"]
    )
    new_token = resp.json()["error"]["confirm_token"]
    # 換參數（reason）帶新 token：不符
    resp = post(
        client, "/v1/note_delete", **body, reason="別的", confirm_token=new_token
    )
    assert resp.status_code == 400 and code(resp) == "invalid_confirm_token"
    done = ok(post(client, "/v1/note_delete", **body, confirm_token=new_token))
    assert done["executed"] is True


@pytest.mark.parametrize("path", ["/v1/topics"])
def test_new_endpoints_reject_unknown_fields(client, path):
    create_vault(client, DEV)
    resp = post(client, path, space="dev", vault=DEV, bogus=1)
    assert resp.status_code == 422


# ── 10. 墓碑清除後：還原回 404、墓碑列表不再出現、doctor 綠 ──


def _age_tombstones(db_path) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute("UPDATE note_tombstones SET deleted_at = ?", (OLD,))
            conn.execute("UPDATE document_tombstones SET deleted_at = ?", (OLD,))
    finally:
        conn.close()


OLD = "2026-01-01T00:00:00.000Z"


def test_restore_after_purge_is_not_found(doc_client, doc_worker, db_path):
    client = doc_client
    note = write_note(client, DEV, "要刪的", "內容")
    up = _upload(client, "notes.md", "# 標題\n\n內容".encode())
    doc_worker()
    confirm(client, "/v1/note_delete", space="dev", vault=DEV, id=note["id"])
    confirm(client, "/v1/document_delete", space="dev", vault=DEV, id=up["document_id"])
    listed = ok(post(client, "/v1/tombstones", space="dev", vault=DEV))["items"]
    assert {i["id"] for i in listed} == {note["id"], up["document_id"]}
    _age_tombstones(db_path)
    conn = connect(db_path)
    try:
        _, done = admin.purge_tombstones(conn, 30)
    finally:
        conn.close()
    assert done["note_tombstones"] == 1 and done["document_tombstones"] == 1
    resp = post(client, "/v1/note_undelete", space="dev", id=note["id"])
    assert resp.status_code == 404 and code(resp) == "not_found"
    resp = post(client, "/v1/document_undelete", space="dev", id=up["document_id"])
    assert resp.status_code == 404 and code(resp) == "not_found"
    assert ok(post(client, "/v1/tombstones", space="dev", vault=DEV))["items"] == []
    report = ok(client.post("/v1/status"))["doctor"]
    failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
    assert failed == []
    summary = next(c for c in report["checks"] if c["name"] == "tombstones.summary")
    assert summary["status"] == "pass"
