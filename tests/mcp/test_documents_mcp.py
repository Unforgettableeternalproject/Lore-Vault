"""T-67／T-69：MCP `upload`（本機路徑白名單、symlink／`..` 逃逸）、文件的降級行為。"""

from __future__ import annotations

import os

import httpx2
import pytest

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, Secret
from lore_vault.documents.worker import DocumentWorker
from lore_vault.mcp.upload import UploadPathError, resolve_upload_path
from lore_vault.schema import Vault
from lore_vault.storage.blobs import BlobStore
from lore_vault.storage.db import connect
from lore_vault.storage.vaults import upsert_vault

from .conftest import (
    DIM,
    TOKEN,
    NullEmbedder,
    Session,
    add_vault,
    asgi,
    failing,
    make_shell,
    session,
)

pytestmark = pytest.mark.anyio

FOLDER_VAULT = "folder/alpha"


class Embed:
    model = "fake"

    def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (DIM - 1)


@pytest.fixture
def blob_dir(tmp_path):
    return tmp_path / "blobs"


@pytest.fixture
def doc_app(db_path, blob_dir):
    return create_app(
        ApiSettings(
            db_path=db_path,
            snapshot_cache_dir=db_path.parent / "snapshot-cache",
            token=Secret(TOKEN),
            config=Config(
                embedding=EmbeddingConfig(dim=DIM),
                documents=DocumentsConfig(blob_dir=str(blob_dir), max_file_bytes=4096),
            ),
            query_embedder=NullEmbedder(),
            enrich_worker=False,
            document_worker=False,
            embedding_warmup=False,
        )
    )


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "Alpha"
    path.mkdir()
    (path / "docs").mkdir()
    (path / "docs" / "設定.md").write_text("# 部署\n\nzeppelin 流程", encoding="utf-8")
    return path


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text("不該被讀到的秘密", encoding="utf-8")
    return path


def _shell(transport, project, snapshot_dir=None, **overrides):
    shell = make_shell(transport, snapshot_dir, **overrides)
    shell._cwd = lambda: str(project)
    return shell


def _run_worker(db_path, blob_dir):
    conn = connect(db_path)
    try:
        DocumentWorker(
            conn,
            Config(
                embedding=EmbeddingConfig(dim=DIM),
                documents=DocumentsConfig(blob_dir=str(blob_dir)),
            ),
            blobs=BlobStore(blob_dir),
            embedder=Embed(),
        ).run_once()
    finally:
        conn.close()


# ── upload ─────────────────────────────────────────────────────────


async def test_upload_relative_path_with_cwd_binding(doc_app, db_path, project):
    add_vault(db_path, FOLDER_VAULT, display="Alpha")
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        result = await s.ok("upload", path="docs/設定.md")
        assert result["vault"] == FOLDER_VAULT and result["space"] == "dev"
        assert result["vault_source"] == "cwd_binding"
        assert result["status"] == "pending" and result["filename"] == "設定.md"
        again = await s.ok("upload", path=str(project / "docs" / "設定.md"))
        assert again["duplicate"] is True
        assert again["document_id"] == result["document_id"]


async def test_upload_explicit_vault_in_lore_space(doc_app, db_path, project):
    conn = connect(db_path)
    try:
        upsert_vault(conn, Vault(key="lore/world", display="w", space="lore"))
    finally:
        conn.close()
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        await s.ok("space", action="set", value="lore")
        err = await s.err("upload", path="docs/設定.md")
        assert err["error"]["code"] == "vault_required"
        result = await s.ok("upload", path="docs/設定.md", vault="lore/world")
        assert result["space"] == "lore" and result["vault_source"] == "explicit"


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda project, outside: str(outside / "secret.txt"),
        lambda project, outside: "docs/../../outside/secret.txt",
        lambda project, outside: str(project / "docs" / ".." / "docs" / "設定.md"),
        lambda project, outside: "..\\outside\\secret.txt",
    ],
    ids=["absolute_outside", "dotdot_escape", "dotdot_inside", "backslash_dotdot"],
)
async def test_upload_rejects_paths_outside_roots(
    doc_app, db_path, project, outside, path_factory
):
    add_vault(db_path, FOLDER_VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        err = await s.err(
            "upload", path=path_factory(project, outside), vault=FOLDER_VAULT
        )
        assert err["error"]["code"] == "path_not_allowed"
        assert "不該被讀到" not in str(err)


async def test_upload_rejects_symlink_escape(doc_app, db_path, project, outside):
    link = project / "docs" / "link.txt"
    try:
        os.symlink(outside / "secret.txt", link)
    except OSError as exc:  # Windows 無權限建立 symlink
        pytest.skip(f"無法建立 symlink：{exc}")
    add_vault(db_path, FOLDER_VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        err = await s.err("upload", path="docs/link.txt", vault=FOLDER_VAULT)
        assert err["error"]["code"] == "path_not_allowed"


def test_symlink_escape_is_checked_on_realpath(project, outside, monkeypatch):
    """不依賴建 symlink 權限：realpath 指到白名單外即拒絕（拿掉 realpath 比對會紅）。"""
    import lore_vault.mcp.upload as upload_mod

    target = str(project / "docs" / "設定.md")
    real = os.path.realpath

    def fake_realpath(path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(target):
            return str(outside / "secret.txt")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(upload_mod.os.path, "realpath", fake_realpath)
    with pytest.raises(UploadPathError) as info:
        resolve_upload_path("docs/設定.md", [project], cwd=str(project))
    assert info.value.code == "path_not_allowed"


async def test_extra_upload_roots_are_allowed(doc_app, db_path, project, outside):
    add_vault(db_path, FOLDER_VAULT)
    shell = _shell(asgi(doc_app), project, upload_roots=(outside,))
    async with session(shell) as client:
        s = Session(client)
        result = await s.ok(
            "upload", path=str(outside / "secret.txt"), vault=FOLDER_VAULT
        )
        assert result["status"] == "pending"


async def test_upload_size_and_missing_file(doc_app, db_path, project):
    add_vault(db_path, FOLDER_VAULT)
    (project / "big.txt").write_bytes(b"x" * 5000)
    shell = _shell(asgi(doc_app), project, max_upload_bytes=4096)
    async with session(shell) as client:
        s = Session(client)
        err = await s.err("upload", path="big.txt", vault=FOLDER_VAULT)
        assert err["error"]["code"] == "too_large"
        err = await s.err("upload", path="nope.txt", vault=FOLDER_VAULT)
        assert err["error"]["code"] == "file_not_found"
        err = await s.err("upload", path="docs", vault=FOLDER_VAULT)
        assert err["error"]["code"] == "not_a_file"
    # 殼端上限放寬時，服務端同一上限仍會擋（413 → too_large）
    shell = _shell(asgi(doc_app), project, max_upload_bytes=10**6)
    async with session(shell) as client:
        err = await Session(client).err("upload", path="big.txt", vault=FOLDER_VAULT)
        assert err["error"]["code"] == "too_large" and err["http_status"] == 413


async def test_upload_fails_when_unreachable(project, snapshot_dir):
    shell = _shell(
        failing(lambda r: httpx2.ConnectError("refused", request=r)),
        project,
        snapshot_dir,
    )
    async with session(shell) as client:
        err = await Session(client).err("upload", path="docs/設定.md", vault="x/y")
        assert err["error"]["code"] == "service_unreachable"


# ── 降級：快照不含文件（T-69）──────────────────────────────────────


async def test_degraded_marks_documents_unsupported(
    doc_app, db_path, blob_dir, project, snapshot_dir
):
    add_vault(db_path, FOLDER_VAULT)
    online = _shell(asgi(doc_app), project, snapshot_dir)
    async with session(online) as client:
        s = Session(client)
        note = await s.ok(
            "write", vault=FOLDER_VAULT, title="zeppelin 筆記", body="zeppelin"
        )
        doc = await s.ok("upload", path="docs/設定.md", vault=FOLDER_VAULT)
        _run_worker(db_path, blob_dir)
        live = await s.ok("recall", vault=FOLDER_VAULT, query="zeppelin")
        assert {i["kind"] for i in live["items"]} == {"note", "chunk"}
        assert await online.refresh_snapshot() is not None

    offline = _shell(
        failing(lambda r: httpx2.ConnectError("refused", request=r)),
        project,
        snapshot_dir,
    )
    async with session(offline) as client:
        s = Session(client)
        recalled = await s.ok("recall", vault=FOLDER_VAULT, query="zeppelin")
        assert recalled["degraded"] is True
        assert recalled["unsupported_kinds"] == ["chunk"]
        assert [i["id"] for i in recalled["items"]] == [note["id"]]
        only_chunks = await s.ok(
            "recall", vault=FOLDER_VAULT, query="zeppelin", kinds=["chunk"]
        )
        assert only_chunks["items"] == [] and only_chunks["unsupported_kinds"] == [
            "chunk"
        ]
        chunk_id = "chunk:" + doc["document_id"].removeprefix("doc:") + ":0"
        got = await s.ok(
            "get", vault=FOLDER_VAULT, ids=[note["id"], doc["document_id"], chunk_id]
        )
        assert [i["id"] for i in got["items"]] == [note["id"]]
        assert got["unavailable"] == [doc["document_id"], chunk_id]
        assert got["missing"] == []
        listed = await s.ok("list", vault=FOLDER_VAULT)
        assert listed["unsupported_kinds"] == ["document"]
        assert [i["id"] for i in listed["items"]] == [note["id"]]
