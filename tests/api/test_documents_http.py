"""T-63～T-67 端到端（HTTP）：upload → 背景抽取 → 切段 → 索引 → recall 命中 chunk
（含 locator）→ get 整份／單段；重複上傳、重試、新版本、大小上限、錯誤碼。

樣本全部由程式產生（`tests/samples.py`）。worker 不在背景跑，由測試手動跑一輪，
embedder 是假的（不打 Ollama）。
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, WorkerConfig
from lore_vault.documents.service import upload_mime
from lore_vault.documents.worker import DocumentWorker
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect

from ..samples import docx_bytes, pdf_bytes, pptx_bytes
from .conftest import DIM, OMIT, FakeEmbedder, create_vault, write_note

A = "folder/docs-a"
MAX_BYTES = 200_000


def _config(blob_dir, max_bytes=MAX_BYTES) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=max_bytes),
    )


@pytest.fixture
def blob_dir(tmp_path):
    return tmp_path / "blobs"


@pytest.fixture
def docs(make_client, blob_dir):
    client = make_client(config=_config(blob_dir), document_worker=False)
    create_vault(client, A)
    return client


@pytest.fixture
def run_worker(db_path, blob_dir):
    def run(rounds: int = 1):
        conn = connect(db_path)
        try:
            worker = DocumentWorker(
                conn,
                _config(blob_dir),
                blobs=BlobStore(blob_dir),
                embedder=FakeEmbedder(),
            )
            return [worker.run_once() for _ in range(rounds)]
        finally:
            conn.close()

    return run


def upload(client, data: bytes, filename: str, *, vault=A, space="dev", **fields):
    form = {"vault": vault, "space": space, **fields}
    form = {k: v for k, v in form.items() if v is not OMIT}
    return client.post(
        "/v1/documents", data=form, files={"file": (filename, data, "x/unknown")}
    )


def uploaded(client, data: bytes, filename: str, **kw) -> dict:
    resp = upload(client, data, filename, **kw)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def recall(client, query: str, **kw) -> dict:
    resp = client.post("/v1/recall", json={"query": query, "vault": A, **kw})
    assert resp.status_code == 200, resp.text
    return resp.json()


def get(client, ids, **kw) -> dict:
    resp = client.post("/v1/get", json={"vault": A, "ids": ids, **kw})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 端到端：各格式 ─────────────────────────────────────────────────

SAMPLES = [
    (
        "設定.md",
        "# 部署\n\n## 資料庫\n\n備份用 VACUUM INTO，保留七份。\n".encode(),
        "VACUUM",
        {"kind": "heading", "value": "部署 > 資料庫"},
    ),
    ("說明.txt", "純文字說明：zeppelin 協定的握手流程。".encode(), "zeppelin", None),
    (
        "程式.py",
        b"def zeppelin_handshake():\n    return 42\n",
        "zeppelin_handshake",
        None,
    ),
    ("設定.json", b'{"server": {"codename": "zeppelin"}}', "zeppelin", None),
    ("設定.yaml", b"server:\n  codename: zeppelin\n", "zeppelin", None),
    ("設定.toml", b'[server]\ncodename = "zeppelin"\n', "zeppelin", None),
    (
        "報告.pdf",
        pdf_bytes(
            [
                "Quarterly report introduction and overall summary of the plan.",
                "The zeppelin handshake protocol is described on this second page.",
            ]
        ),
        "zeppelin",
        {"kind": "page", "value": 2},
    ),
    (
        "規格.docx",
        docx_bytes([("概述", "專案背景說明"), ("協定", "zeppelin 握手流程細節")]),
        "zeppelin",
        {"kind": "heading", "value": "協定"},
    ),
    (
        "簡報.pptx",
        pptx_bytes([("封面", "年度計畫"), ("協定", "zeppelin 握手")]),
        "zeppelin",
        {"kind": "slide", "value": 2},
    ),
]


@pytest.mark.parametrize(
    "filename, data, query, locator", SAMPLES, ids=[s[0] for s in SAMPLES]
)
def test_upload_extract_index_recall_get(
    docs, run_worker, filename, data, query, locator
):
    created = uploaded(docs, data, filename)
    assert created["status"] == "pending" and created["duplicate"] is False
    assert created["vault"] == A and created["space"] == "dev"
    [stats] = run_worker()
    assert stats.extract.done == 1, stats.to_dict()

    result = recall(docs, query, kinds=["chunk"])
    assert result["kinds"] == ["chunk"] and result["items"], result
    hit = result["items"][0]
    assert hit["kind"] == "chunk" and hit["document_id"] == created["document_id"]
    assert hit["title"] == filename and hit["vault"] == A
    assert hit["chunk_id"] == hit["id"] and hit["id"].startswith("chunk:")
    assert hit["summary_source"] == "excerpt" and query in hit["summary"]
    assert "body" not in hit and "text" not in hit
    if locator is not None:
        assert hit["locator"] == locator
    else:
        assert hit["locator"]["kind"] == "offset"

    got = get(docs, [hit["id"], created["document_id"]])
    chunk_item, doc_item = got["items"]
    assert chunk_item["kind"] == "chunk" and query in chunk_item["text"]
    assert chunk_item["locator"] == hit["locator"]
    assert doc_item["kind"] == "document" and doc_item["status"] == "ready"
    assert query in doc_item["text"] and not doc_item["truncated"]
    assert doc_item["chunk_count"] >= 1 and got["missing"] == []


def test_recall_mixes_notes_and_chunks_by_default(docs, run_worker):
    note = write_note(docs, A, "zeppelin 筆記", "筆記裡也提到 zeppelin 協定")
    doc = uploaded(docs, "文件寫著 zeppelin 協定的細節".encode(), "a.md")
    run_worker()
    result = recall(docs, "zeppelin")
    kinds = {(i["kind"], i["id"]) for i in result["items"]}
    assert ("note", note["id"]) in kinds
    assert any(k == "chunk" for k, _ in kinds)
    assert result["kinds"] == ["note", "chunk"] and result["unsupported_kinds"] == []
    assert result["missing_chunk_embeddings"] == 0
    only_notes = recall(docs, "zeppelin", kinds=["note"])
    assert {i["kind"] for i in only_notes["items"]} == {"note"}
    only_chunks = recall(docs, "zeppelin", kinds=["chunk"])
    assert {i["document_id"] for i in only_chunks["items"]} == {doc["document_id"]}


def test_vector_only_chunk_can_rank(make_client, blob_dir, db_path):
    """字面不相符、向量相近的 chunk 仍可經 chunk 向量那一路進前幾名（四路 RRF）。"""

    class Semantic:
        model = "semantic"

        def embed(self, text: str) -> list[float]:
            vec = [0.0] * DIM
            vec[0 if ("貓" in text or "寵物" in text) else 1] = 1.0
            return vec

    client = make_client(
        config=_config(blob_dir), document_worker=False, query_embedder=Semantic()
    )
    create_vault(client, A)
    doc = uploaded(client, "家裡的貓咪很愛睡覺".encode(), "日記.txt")
    uploaded(client, "今天天氣晴朗".encode(), "天氣.txt")
    conn = connect(db_path)
    try:
        DocumentWorker(
            conn, _config(blob_dir), blobs=BlobStore(blob_dir), embedder=Semantic()
        ).run_once()
    finally:
        conn.close()
    result = client.post(
        "/v1/recall", json={"query": "寵物", "vault": A, "kinds": ["chunk"]}
    ).json()
    assert result["items"][0]["document_id"] == doc["document_id"]
    lexical = client.post(
        "/v1/recall",
        json={"query": "寵物", "vault": A, "kinds": ["chunk"], "mode": "lexical"},
    ).json()
    assert lexical["items"] == []


def test_get_document_text_is_budgeted_and_rejoins_overlap(docs, run_worker):
    text = "".join(f"第{i}段文件內容，說明切段與重疊的還原。" for i in range(80))
    doc = uploaded(docs, text.encode(), "長文.txt")
    run_worker()
    full = get(docs, [doc["document_id"]], budget=100000)["items"][0]
    assert full["text"] == text and full["chunk_count"] > 1
    cut = get(docs, [doc["document_id"]], budget=100)
    item = cut["items"][0]
    assert cut["truncated"] and item["truncated"] and len(item["text"]) == 100
    assert item["text_chars"] == len(text) and cut["used_chars"] == 100


# ── 重複上傳、重試、新版本 ─────────────────────────────────────────


def _rows(db_path) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    finally:
        conn.close()


def test_same_content_returns_existing_document(docs, run_worker, db_path):
    first = uploaded(docs, "內容".encode(), "a.md")
    resp = upload(docs, "內容".encode(), "改名.md")
    assert resp.status_code == 200
    again = resp.json()
    assert again["duplicate"] is True and again["document_id"] == first["document_id"]
    run_worker()
    after_ready = upload(docs, "內容".encode(), "a.md").json()
    assert after_ready["duplicate"] is True and after_ready["status"] == "ready"
    [stats] = run_worker()
    assert stats.extract.done == 0  # 沒有重新排隊
    assert _rows(db_path) == 1


def test_failed_document_is_retried_in_place(docs, run_worker, db_path):
    broken = b"PK\x03\x04 definitely not a docx"
    first = uploaded(docs, broken, "壞.docx")
    run_worker()
    listed = docs.post("/v1/list", json={"vault": A, "kinds": ["document"]}).json()
    assert listed["items"][0]["status"] == "failed"
    assert listed["items"][0]["error_code"] == "corrupt"
    resp = upload(docs, broken, "壞.docx")
    assert resp.status_code == 201
    retried = resp.json()
    assert retried["retried"] is True and retried["duplicate"] is False
    assert retried["document_id"] == first["document_id"]
    assert retried["status"] == "pending" and _rows(db_path) == 1


def _document_mime(client, document_id: str) -> str:
    listed = client.post("/v1/list", json={"vault": A, "kinds": ["document"]}).json()
    [item] = [i for i in listed["items"] if i["id"] == document_id]
    return item["mime"]


OCTET = "application/octet-stream"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(
    ("data", "filename", "expected"),
    [
        ("純文字內容".encode(), "筆記.txt", "text/plain"),
        ("# 標題".encode(), "說明.MD", "text/markdown"),
        (b"print(1)\n", "tool.py", "text/plain"),
        (docx_bytes([("概述", "段落一")]), "報告.docx", DOCX_MIME),
    ],
    ids=["txt", "md", "py", "docx"],
)
def test_octet_stream_mime_is_inferred_from_extension(docs, data, filename, expected):
    # MCP 客戶端預設送 octet-stream：依副檔名推定，下載端才有正確 Content-Type
    doc = uploaded(docs, data, filename, mime=OCTET)
    assert _document_mime(docs, doc["document_id"]) == expected


def test_explicit_mime_is_kept(docs):
    doc = uploaded(docs, "內容".encode(), "筆記.txt", mime="text/x-custom")
    assert _document_mime(docs, doc["document_id"]) == "text/x-custom"


def test_retry_infers_mime_from_new_filename(docs, run_worker):
    broken = b"PK\x03\x04 definitely not a docx"
    first = uploaded(docs, broken, "壞.docx", mime="x/first")
    run_worker()
    retried = uploaded(docs, broken, "壞.docx", mime=OCTET)
    assert retried["retried"] is True
    assert retried["document_id"] == first["document_id"]
    assert _document_mime(docs, first["document_id"]) == DOCX_MIME


@pytest.mark.parametrize(
    ("filename", "mime", "expected"),
    [
        ("a.txt", None, "text/plain"),
        ("a.yml", "  ", "application/yaml"),
        ("a.pdf", "Application/Octet-Stream", "application/pdf"),
        ("Dockerfile", OCTET, "text/plain"),
        ("a.txt", "text/plain; charset=utf-8", "text/plain; charset=utf-8"),
        ("photo.png", OCTET, OCTET),
        ("photo.png", None, OCTET),
    ],
)
def test_upload_mime_rules(filename, mime, expected):
    assert upload_mime(filename, mime) == expected


def test_same_filename_new_content_is_new_version(docs, run_worker):
    v1 = uploaded(docs, "舊版：zeppelin 使用 v1 握手".encode(), "協定.md")
    run_worker()
    v2 = uploaded(docs, "新版：zeppelin 改用 v2 握手".encode(), "協定.md")
    assert v2["supersedes"] == v1["document_id"] and v2["version"] == 2
    run_worker()
    hits = recall(docs, "zeppelin", kinds=["chunk"])["items"]
    assert {h["document_id"] for h in hits} == {v2["document_id"]}
    listed = docs.post("/v1/list", json={"vault": A, "kinds": ["document"]}).json()
    by_id = {i["id"]: i for i in listed["items"]}
    assert by_id[v1["document_id"]]["superseded_by"] == v2["document_id"]
    assert by_id[v2["document_id"]]["superseded_by"] is None
    # 舊版仍可 get，並標示被取代
    old = get(docs, [v1["document_id"]])["items"][0]
    assert "v1" in old["text"] and old["superseded_by"] == v2["document_id"]
    # 改回舊內容：舊列已被取代，視為新版本而不是 duplicate
    v3 = uploaded(docs, "舊版：zeppelin 使用 v1 握手".encode(), "協定.md")
    assert v3["duplicate"] is False and v3["version"] == 3
    assert v3["supersedes"] == v2["document_id"]


# ── 大小上限與錯誤 ─────────────────────────────────────────────────


def test_content_length_over_limit_is_rejected_before_reading(make_client, blob_dir):
    client = make_client(
        config=_config(blob_dir, max_bytes=1000), document_worker=False
    )
    create_vault(client, A)
    resp = upload(client, b"x" * 70_000, "big.txt")
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "too_large"
    assert not blob_dir.exists() or not any(blob_dir.rglob("*"))


def test_streamed_body_over_limit_is_cut_while_reading(make_client, blob_dir):
    """沒有 Content-Length（chunked）時邊讀邊數，超過就中止。"""
    client = make_client(
        config=_config(blob_dir, max_bytes=1000), document_worker=False
    )
    create_vault(client, A)
    boundary = "lorevaultboundary"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="vault"\r\n\r\n'
        f"{A}\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="space"\r\n\r\ndev\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n'
    ).encode()
    sent: list[int] = []

    def body():
        yield head
        for _ in range(200):
            sent.append(1)
            yield b"x" * 1024
        yield f"\r\n--{boundary}--\r\n".encode()

    resp = client.post(
        "/v1/documents",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 413


def test_file_just_over_limit_in_small_request_is_rejected(make_client, blob_dir):
    client = make_client(
        config=_config(blob_dir, max_bytes=1000), document_worker=False
    )
    create_vault(client, A)
    assert upload(client, b"x" * 1000, "ok.txt").status_code == 201
    resp = upload(client, b"y" * 1001, "big.txt")
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "too_large"


@pytest.mark.parametrize(
    "fields, files, code",
    [
        ({"vault": A}, {"file": ("a.md", b"x")}, "space_required"),
        ({"vault": A, "space": "nope"}, {"file": ("a.md", b"x")}, "invalid_space"),
        ({"space": "dev"}, {"file": ("a.md", b"x")}, "vault_required"),
        ({"vault": A, "space": "dev"}, {}, "invalid_request"),
        (
            {"vault": A, "space": "dev", "vaul": "x"},
            {"file": ("a.md", b"x")},
            "invalid_request",
        ),
        (
            {"vault": A, "space": "dev"},
            {"file": ("a.exe", b"MZ")},
            "unsupported_format",
        ),
    ],
)
def test_upload_rejections(docs, blob_dir, fields, files, code):
    resp = docs.post("/v1/documents", data=fields, files=files or None)
    if not files:
        resp = docs.post(
            "/v1/documents",
            data=fields,
            files={"other": ("x", b"x")},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == code
    assert not blob_dir.exists() or not any(blob_dir.rglob("*"))


def test_unknown_vault_and_other_space_are_404(docs):
    assert upload(docs, b"x", "a.md", vault="folder/nope").status_code == 404
    create_vault(docs, "lore/world", space="lore")
    resp = upload(docs, b"x", "a.md", vault="lore/world", space="dev")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "unknown_vault"
    assert (
        upload(docs, b"x", "a.md", vault="lore/world", space="lore").status_code == 201
    )


def test_documents_disabled_without_blob_dir(client):
    create_vault(client, A)
    resp = upload(client, b"x", "a.md")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "documents_not_configured"


def test_non_multipart_is_rejected(docs):
    resp = docs.post(
        "/v1/documents", content=b"{}", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400


# ── list 與 status ────────────────────────────────────────────────


def test_list_merges_notes_and_documents_with_pagination(docs, run_worker):
    for i in range(3):
        write_note(docs, A, f"筆記 {i}", "內容")
        uploaded(docs, f"文件 {i}".encode(), f"doc{i}.md")
    seen: list[str] = []
    cursor = None
    while True:
        body = {"vault": A, "limit": 2}
        if cursor:
            body["cursor"] = cursor
        page = docs.post("/v1/list", json=body).json()
        seen.extend(i["id"] for i in page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 6
    kinds = docs.post("/v1/list", json={"vault": A, "kinds": ["document"]}).json()
    assert {i["kind"] for i in kinds["items"]} == {"document"}
    topics = docs.post("/v1/list", json={"vault": A, "topics": ["x"]}).json()
    assert all(i["kind"] == "note" for i in topics["items"])
    bad = docs.post("/v1/list", json={"vault": A, "kinds": ["episode"]})
    assert bad.status_code == 400


def test_status_reports_documents_and_doctor(docs, run_worker):
    uploaded(docs, "內容".encode(), "a.md")
    run_worker()
    data = docs.post("/v1/status", json={}).json()
    assert data["documents"]["enabled"] is True
    assert data["documents"]["worker"] == {"enabled": False, "running": False}
    names = {c["name"]: c["status"] for c in data["doctor"]["checks"]}
    for name in (
        "documents.blob_exists",
        "documents.chunk_count_matches",
        "documents.fts_rows_match_chunks",
        "documents.superseded_chunks_removed",
        "documents.stuck_processing",
    ):
        assert names[name] == "pass", (name, json.dumps(data["doctor"])[:500])


# ── 服務程序內的背景文件 worker ────────────────────────────────────


def test_background_document_worker_is_woken_by_upload(make_client, blob_dir, caplog):
    config = Config(
        embedding=EmbeddingConfig(dim=DIM),
        worker=WorkerConfig(poll_interval=60.0),
        documents=DocumentsConfig(blob_dir=str(blob_dir)),
    )

    def factory(conn, should_stop):
        return DocumentWorker(
            conn,
            config,
            blobs=BlobStore(blob_dir),
            embedder=FakeEmbedder(),
            should_stop=should_stop,
        )

    caplog.set_level(logging.INFO, logger="lore_vault.api.worker")
    client = make_client(config=config, document_worker_factory=factory)
    create_vault(client, A)
    status = client.post("/v1/status", json={}).json()
    assert status["documents"]["worker"]["enabled"] is True
    doc = uploaded(client, "背景抽取 zeppelin".encode(), "a.md")

    def ready() -> bool:
        listed = client.post("/v1/list", json={"vault": A, "kinds": ["document"]})
        return listed.json()["items"][0]["status"] == "ready"

    deadline = time.monotonic() + 5
    while not ready() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready()
    hits = recall(client, "zeppelin", kinds=["chunk"])["items"]
    assert hits[0]["document_id"] == doc["document_id"]
    assert any("文件本輪：抽取 完成 1" in r.getMessage() for r in caplog.records)


def test_document_worker_off_without_blob_dir(client):
    status = client.post("/v1/status", json={}).json()
    assert status["documents"] == {
        "enabled": False,
        "worker": {"enabled": False, "running": False},
        "backlog": status["documents"]["backlog"],
    }
