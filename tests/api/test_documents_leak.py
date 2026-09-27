"""A5／A18 延伸到文件路徑：vault A 與 dev space 的請求拿不到 vault B 或 lore 的文件。

- `test_documents_do_not_leak`：recall（lexical／vector／hybrid、vault='*'）、get（doc
  與 chunk id）、list、upload 去重，全部不得越界
- `test_documents_leak_test_is_load_bearing`：把儲存層的 vault 條件換成恆真，
  同一組嘗試必須抓到洩漏（證明測試本身會紅）
"""

from __future__ import annotations

import pytest

from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig
from lore_vault.documents.worker import DocumentWorker
from lore_vault.storage import chunk_vectors, documents, fts, notes, vectors
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect

from .conftest import DIM, FakeEmbedder, create_vault

A = "folder/doc-leak-a"
B = "folder/doc-leak-b"
LORE = "lore/doc-leak"
SHARED = "共同關鍵字 zeppelin 世界觀"


def _config(blob_dir) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(blob_dir)),
    )


def _upload(client, vault, space, filename, text):
    resp = client.post(
        "/v1/documents",
        data={"vault": vault, "space": space},
        files={"file": (filename, text.encode(), "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


@pytest.fixture
def world(make_client, db_path, tmp_path):
    blob_dir = tmp_path / "blobs"
    client = make_client(config=_config(blob_dir), document_worker=False)
    create_vault(client, A)
    create_vault(client, B)
    create_vault(client, LORE, space="lore")
    a = _upload(client, A, "dev", "a.md", f"A 的文件 {SHARED}")
    b = _upload(client, B, "dev", "b.md", f"B 的秘密文件 {SHARED}")
    lore = _upload(client, LORE, "lore", "lore.md", f"世界觀秘密 {SHARED}")
    conn = connect(db_path)
    try:
        DocumentWorker(
            conn, _config(blob_dir), blobs=BlobStore(blob_dir), embedder=FakeEmbedder()
        ).run_once()
    finally:
        conn.close()
    return client, {"a": a, "b": b, "lore": lore}


def _chunk(doc_id: str) -> str:
    return "chunk:" + doc_id.removeprefix("doc:") + ":0"


def _leaks(client, ids) -> list[str]:
    found: list[str] = []
    foreign = {ids["b"], ids["lore"]}
    for mode in ("lexical", "vector", "hybrid"):
        data = client.post(
            "/v1/recall",
            json={"query": SHARED, "vault": A, "mode": mode, "kinds": ["chunk"]},
        ).json()
        if any(i["document_id"] in foreign or i["vault"] != A for i in data["items"]):
            found.append(f"recall:{mode}")
    star = client.post(
        "/v1/recall", json={"query": SHARED, "vault": "*", "kinds": ["chunk"]}
    ).json()
    if any(i["document_id"] == ids["lore"] for i in star["items"]):
        found.append("recall:star_space")

    refs = [ids["b"], _chunk(ids["b"]), ids["lore"], _chunk(ids["lore"])]
    data = client.post("/v1/get", json={"vault": A, "ids": [ids["a"], *refs]}).json()
    if any(i["id"] != ids["a"] for i in data["items"]) or data["missing"] != refs:
        found.append("get")
    star_get = client.post(
        "/v1/get", json={"vault": "*", "ids": [ids["lore"], _chunk(ids["lore"])]}
    ).json()
    if star_get["items"]:
        found.append("get:star_space")

    data = client.post("/v1/list", json={"vault": A, "kinds": ["document"]}).json()
    if {i["id"] for i in data["items"]} != {ids["a"]}:
        found.append("list")
    star_list = client.post(
        "/v1/list", json={"vault": "*", "kinds": ["document"]}
    ).json()
    if ids["lore"] in {i["id"] for i in star_list["items"]}:
        found.append("list:star_space")

    # 同內容上傳到 A：B 的同內容文件不可被當成 A 的 duplicate
    resp = client.post(
        "/v1/documents",
        data={"vault": A, "space": "dev"},
        files={"file": ("b.md", f"B 的秘密文件 {SHARED}".encode(), "text/plain")},
    ).json()
    if resp["duplicate"] or resp["document_id"] == ids["b"]:
        found.append("upload.duplicate")
    return found


def test_documents_do_not_leak(world):
    client, ids = world
    assert _leaks(client, ids) == []
    # 各自的 vault／space 內都看得到自己的文件
    own = client.post(
        "/v1/get", json={"vault": B, "ids": [ids["b"], _chunk(ids["b"])]}
    ).json()
    assert len(own["items"]) == 2
    client.space = "lore"
    lore = client.post(
        "/v1/recall", json={"query": SHARED, "vault": "*", "kinds": ["chunk"]}
    ).json()
    assert {i["document_id"] for i in lore["items"]} == {ids["lore"]}


def test_documents_leak_test_is_load_bearing(world, monkeypatch):
    client, ids = world

    def no_filter(scope, column):
        return "1 = 1", ()

    for module in (notes, fts, vectors, documents, chunk_vectors):
        monkeypatch.setattr(module, "vault_clause", no_filter)
    assert set(_leaks(client, ids)) == {
        "recall:lexical",
        "recall:vector",
        "recall:hybrid",
        "recall:star_space",
        "get",
        "get:star_space",
        "list",
        "list:star_space",
        "upload.duplicate",
    }
