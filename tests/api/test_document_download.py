"""`POST /v1/document_download`：原始檔取回、範圍（vault／space）、大小上限、blob 驗證、
bearer 與 UI session 兩種認證。

範圍與上限的測試刻意寫成「拿掉保護就會紅」：跨 vault／跨 space 一律 404、超過
`max_bytes` 在讀 blob 之前就 413（blob 已刪也照樣 413）。
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, UiConfig
from lore_vault.storage.blobs import BlobStore

from .conftest import AUTH, DIM, UI_PASSWORD, UI_USER, make_settings, seed_ui_account

DEV = "folder/dl-a"
OTHER = "folder/dl-b"
LORE = "lore/dl-world"
UI = {"X-Lore-Vault-UI": "1"}
CONTENT = "# 標題\n\n世界觀設定：zeppelin 流程\n".encode()


def _config(blob_dir, **ui) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM),
        documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=200_000),
        **({"ui": UiConfig(**ui)} if ui else {}),
    )


@pytest.fixture
def blob_dir(tmp_path):
    return tmp_path / "blobs"


@pytest.fixture
def docs(make_client, blob_dir):
    client = make_client(config=_config(blob_dir), document_worker=False)
    for key, space in ((DEV, "dev"), (OTHER, "dev"), (LORE, "lore")):
        resp = client.post(
            "/v1/vaults", json={"key": key, "display": key, "space": space}
        )
        assert resp.status_code == 201, resp.text
    return client


def _upload(client, name: str, data: bytes, vault: str = DEV, space: str = "dev"):
    resp = client.post(
        "/v1/documents",
        files={"file": (name, data, "text/markdown")},
        data={"vault": vault, "space": space},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _download(client, **body):
    return client.post("/v1/document_download", json=body)


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def test_download_returns_original_bytes_and_headers(docs):
    up = _upload(docs, "設定 notes.md", CONTENT)
    resp = _download(docs, space="dev", vault=DEV, id=up["document_id"])
    assert resp.status_code == 200, resp.text
    assert resp.content == CONTENT
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["x-lore-vault-sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert resp.headers["x-lore-vault-document-id"] == up["document_id"]
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"filename*=UTF-8''{quote('設定 notes.md')}" in disposition
    # ASCII 後備檔名不含非 ASCII 字元
    assert 'filename="__ notes.md"' in disposition
    assert resp.headers["cache-control"] == "no-store"
    # '*' 也可（目前 space 內全部 vault）
    star = _download(docs, space="dev", vault="*", id=up["document_id"])
    assert star.status_code == 200 and star.content == CONTENT


def test_download_is_scoped_to_vault_and_space(docs):
    up = _upload(docs, "a.md", CONTENT)
    lore = _upload(docs, "w.md", b"# lore\n\nsecret world", vault=LORE, space="lore")
    # 同 space 的別的 vault：404
    resp = _download(docs, space="dev", vault=OTHER, id=up["document_id"])
    assert resp.status_code == 404 and _code(resp) == "not_found"
    # 別的 space（即使給 '*'）：404，不洩漏內容
    resp = _download(docs, space="dev", vault="*", id=lore["document_id"])
    assert resp.status_code == 404 and b"secret world" not in resp.content
    resp = _download(docs, space="lore", vault="*", id=up["document_id"])
    assert resp.status_code == 404
    # space 必填、未知欄位 422
    resp = _download(docs, vault=DEV, id=up["document_id"])
    assert resp.status_code == 400 and _code(resp) == "space_required"
    resp = _download(docs, space="dev", vault=DEV, id=up["document_id"], x=1)
    assert resp.status_code == 422


def test_download_max_bytes_refuses_before_reading_blob(docs, blob_dir):
    up = _upload(docs, "a.md", CONTENT)
    # 刪掉 blob：若端點先讀 blob 會變成 500 blob_missing，而不是 413
    BlobStore(blob_dir).path_for(up["sha256"]).unlink()
    resp = _download(
        docs, space="dev", vault=DEV, id=up["document_id"], max_bytes=len(CONTENT) - 1
    )
    assert resp.status_code == 413 and _code(resp) == "too_large"
    resp = _download(docs, space="dev", vault=DEV, id=up["document_id"], max_bytes=0)
    assert resp.status_code == 422


def test_download_verifies_blob(docs, blob_dir):
    up = _upload(docs, "a.md", CONTENT)
    path = BlobStore(blob_dir).path_for(up["sha256"])
    path.write_bytes(b"tampered")
    resp = _download(docs, space="dev", vault=DEV, id=up["document_id"])
    assert resp.status_code == 500 and _code(resp) == "blob_corrupt"
    assert b"tampered" not in resp.content
    path.unlink()
    resp = _download(docs, space="dev", vault=DEV, id=up["document_id"])
    assert resp.status_code == 500 and _code(resp) == "blob_missing"


def test_deleted_document_is_not_downloadable(docs):
    up = _upload(docs, "a.md", CONTENT)
    body = {"space": "dev", "vault": DEV, "id": up["document_id"]}
    planned = docs.post("/v1/document_delete", json=body).json()
    docs.post(
        "/v1/document_delete",
        json={**body, "confirm_token": planned["confirm_token"]},
    ).raise_for_status()
    assert _download(docs, **body).status_code == 404
    docs.post(
        "/v1/document_undelete", json={"space": "dev", "id": up["document_id"]}
    ).raise_for_status()
    resp = _download(docs, **body)
    assert resp.status_code == 200 and resp.content == CONTENT


def test_download_requires_auth(docs, db_path, blob_dir):
    up = _upload(docs, "a.md", CONTENT)
    with TestClient(create_app(make_settings(db_path, config=_config(blob_dir)))) as c:
        resp = c.post(
            "/v1/document_download",
            json={"space": "dev", "vault": DEV, "id": up["document_id"]},
        )
    assert resp.status_code == 401


def test_download_with_ui_session(db_path, blob_dir, tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    seed_ui_account(db_path)
    app = create_app(
        make_settings(
            db_path,
            config=_config(blob_dir, static_dir=str(static)),
            document_worker=False,
        )
    )
    with TestClient(app, base_url="https://testserver") as c:
        resp = c.post(
            "/v1/vaults",
            json={"key": DEV, "display": DEV, "space": "dev"},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        up = c.post(
            "/v1/documents",
            files={"file": ("a.md", CONTENT, "text/markdown")},
            data={"vault": DEV, "space": "dev"},
            headers=AUTH,
        ).json()
        login = c.post(
            "/ui/api/login",
            json={"username": UI_USER, "password": UI_PASSWORD},
            headers=UI,
        )
        assert login.status_code == 204, login.text
        body = {"space": "dev", "vault": DEV, "id": up["document_id"]}
        resp = c.post("/v1/document_download", json=body, headers=UI)
        assert resp.status_code == 200 and resp.content == CONTENT
        # cookie 有效但缺 CSRF header：403
        resp = c.post("/v1/document_download", json=body)
        assert resp.status_code == 403


def test_download_without_blob_dir(client):
    resp = client.post(
        "/v1/document_download", json={"space": "dev", "vault": DEV, "id": "doc:x"}
    )
    assert resp.status_code == 500 and _code(resp) == "documents_not_configured"
