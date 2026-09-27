"""MCP `delete`／`undelete`／`download`（stdio 殼，經 ASGI 接真正的 create_app）。

刻意寫成「拿掉保護就會紅」：
- delete 不帶 confirm_token 不會刪；token 錯、參數不同、過程中資料變動都不會刪
- download 寫檔限在 upload_roots 白名單（絕對路徑、`..`、symlink 逃逸都拒絕）、
  既有檔預設不覆寫、殼端大小上限在收內容時就擋、sha256 不符不落地
HTTP 模式（base64、上限、path 不收）另見 `test_http_mcp.py`。
"""

from __future__ import annotations

import base64
import hashlib
import os

import httpx2
import pytest

from lore_vault.api.app import create_app
from lore_vault.api.settings import ApiSettings
from lore_vault.config import Config, DocumentsConfig, EmbeddingConfig, Secret
from lore_vault.mcp.download import (
    DownloadPathError,
    resolve_download_path,
    safe_filename,
)

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

VAULT = "folder/alpha"
CONTENT = "# 部署\n\nzeppelin 流程\n".encode()


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
    (path / "out").mkdir()
    return path


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


def _shell(transport, project, **overrides):
    shell = make_shell(transport, **overrides)
    shell._cwd = lambda: str(project)
    return shell


async def _upload(s: Session, name: str = "設定.md", data: bytes = CONTENT) -> dict:
    return await s.ok(
        "upload",
        vault=VAULT,
        filename=name,
        content_base64=base64.b64encode(data).decode(),
    )


async def _doc_ids(s: Session) -> set[str]:
    listed = await s.ok("list", vault=VAULT, kinds=["document"])
    return {item["id"] for item in listed["items"]}


async def _note_ids(s: Session) -> set[str]:
    listed = await s.ok("list", vault=VAULT, kinds=["note"])
    return {item["id"] for item in listed["items"]}


# ── delete／undelete ───────────────────────────────────────────────


async def test_delete_document_two_step_and_undelete(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        planned = await s.ok("delete", vault=VAULT, id=doc_id)
        assert planned["executed"] is False and planned["kind"] == "document"
        assert planned["confirm_token"] and "不要自動連打兩步" in planned["next_step"]
        assert planned["plan"]["document_id"] == doc_id
        # 第一步是唯讀：文件仍在
        assert doc_id in await _doc_ids(s)
        done = await s.ok(
            "delete", vault=VAULT, id=doc_id, confirm_token=planned["confirm_token"]
        )
        assert done["executed"] is True and done["kind"] == "document"
        assert "undelete" in done["undelete_hint"]
        assert doc_id not in await _doc_ids(s)
        # 同一個 token 重送不會重複執行
        again = await s.err(
            "delete", vault=VAULT, id=doc_id, confirm_token=planned["confirm_token"]
        )
        assert again["http_status"] == 404
        restored = await s.ok("undelete", id=doc_id)
        assert restored["kind"] == "document"
        assert restored["document"]["id"] == doc_id
        assert restored["document"]["status"] == "pending"
        assert doc_id in await _doc_ids(s)


async def test_delete_note_two_step_and_undelete(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        note = await s.ok("write", vault=VAULT, title="部署流程", body="zeppelin")
        planned = await s.ok("delete", vault=VAULT, id=note["id"], reason="測試")
        assert planned["executed"] is False and planned["kind"] == "note"
        assert note["id"] in await _note_ids(s)
        await s.ok(
            "delete",
            vault=VAULT,
            id=note["id"],
            reason="測試",
            confirm_token=planned["confirm_token"],
        )
        assert note["id"] not in await _note_ids(s)
        restored = await s.ok("undelete", id=note["id"])
        assert restored["kind"] == "note" and restored["restored"] is True
        got = await s.ok("get", vault=VAULT, ids=[note["id"]])
        assert got["items"][0]["body"] == "zeppelin"


async def test_delete_refuses_bad_or_mismatched_token(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        note = await s.ok("write", vault=VAULT, title="保留", body="不可被刪")
        planned = await s.ok("delete", vault=VAULT, id=note["id"])
        token = planned["confirm_token"]
        err = await s.err("delete", vault=VAULT, id=note["id"], confirm_token="x.y")
        assert err["error"]["code"] == "invalid_confirm_token" and err["hint"]
        # 竄改簽章
        tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        err = await s.err("delete", vault=VAULT, id=note["id"], confirm_token=tampered)
        assert err["error"]["code"] == "invalid_confirm_token"
        # 參數與規劃時不同（reason 也綁進 token）
        err = await s.err(
            "delete", vault=VAULT, id=note["id"], reason="別的", confirm_token=token
        )
        assert err["error"]["code"] == "invalid_confirm_token"
        # 兩步之間切換 space：space 也綁在 token 內
        await s.ok("space", action="set", value="lore")
        err = await s.err("delete", vault=VAULT, id=note["id"], confirm_token=token)
        assert err["error"]["code"] == "invalid_confirm_token"
        await s.ok("space", action="set", value="dev")
        assert note["id"] in await _note_ids(s)


async def test_delete_plan_changed_is_not_executed(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        note = await s.ok("write", vault=VAULT, title="舊標題", body="內文")
        planned = await s.ok("delete", vault=VAULT, id=note["id"])
        await s.ok(
            "update",
            vault=VAULT,
            id=note["id"],
            expected_updated=note["updated"],
            title="新標題",
        )
        err = await s.err(
            "delete", vault=VAULT, id=note["id"], confirm_token=planned["confirm_token"]
        )
        assert err["error"]["code"] == "plan_changed"
        # 附新規劃與新 token，但沒有刪
        assert err["error"]["plan"] and err["error"]["confirm_token"]
        assert "使用者" in err["hint"]
        assert note["id"] in await _note_ids(s)


async def test_delete_and_undelete_argument_errors(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        err = await s.err("delete", vault=VAULT, id="chunk:abc:0")
        assert err["error"]["code"] == "invalid_request"
        err = await s.err("undelete", id="chunk:abc:0")
        assert err["error"]["code"] == "invalid_request"
        err = await s.err("delete", vault="*", id="doc:none")
        assert err["http_status"] in (400, 404)
        err = await s.err("undelete", id="doc:none")
        assert err["http_status"] == 404


async def test_file_tools_fail_when_unreachable(project):
    shell = _shell(
        failing(lambda r: httpx2.ConnectError("refused", request=r)), project
    )
    async with session(shell) as client:
        s = Session(client)
        for name, args in (
            ("delete", {"vault": VAULT, "id": "n1"}),
            ("undelete", {"id": "n1"}),
            ("download", {"vault": VAULT, "id": "doc:x"}),
        ):
            err = await s.err(name, **args)
            assert err["error"]["code"] == "service_unreachable", name


# ── download（stdio）────────────────────────────────────────────────


async def test_download_writes_to_cwd_and_refuses_overwrite(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        result = await s.ok("download", vault=VAULT, id=doc_id)
        target = project / "設定.md"
        assert result["path"] == str(target) and result["overwritten"] is False
        assert target.read_bytes() == CONTENT
        assert result["sha256"] == hashlib.sha256(CONTENT).hexdigest()
        assert result["size_bytes"] == len(CONTENT)
        assert result["filename"] == "設定.md"
        assert "content_base64" not in result
        # 既有檔：預設拒絕，原內容不變
        target.write_bytes(b"local edits")
        err = await s.err("download", vault=VAULT, id=doc_id)
        assert err["error"]["code"] == "file_exists" and err["hint"]
        assert target.read_bytes() == b"local edits"
        # 明示覆寫
        result = await s.ok("download", vault=VAULT, id=doc_id, overwrite=True)
        assert result["overwritten"] is True and target.read_bytes() == CONTENT
        assert not [p for p in project.iterdir() if p.name.endswith(".tmp")]


async def test_download_to_directory_and_explicit_file(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        into_dir = await s.ok("download", vault=VAULT, id=doc_id, path="out")
        assert into_dir["path"] == str(project / "out" / "設定.md")
        named = await s.ok("download", vault=VAULT, id=doc_id, path="out/copy.md")
        assert (project / "out" / "copy.md").read_bytes() == CONTENT
        assert named["path"] == str(project / "out" / "copy.md")
        err = await s.err("download", vault=VAULT, id=doc_id, path="missing/x.md")
        assert err["error"]["code"] == "parent_not_found"
        assert not (project / "missing").exists()


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda project, outside: str(outside / "stolen.md"),
        lambda project, outside: "out/../../outside/stolen.md",
        lambda project, outside: "out/../stolen.md",
        lambda project, outside: "..\\outside\\stolen.md",
        lambda project, outside: str(outside),
    ],
    ids=["absolute_outside", "dotdot_escape", "dotdot_inside", "backslash", "dir"],
)
async def test_download_rejects_paths_outside_roots(
    doc_app, db_path, project, outside, path_factory
):
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        err = await s.err(
            "download", vault=VAULT, id=doc_id, path=path_factory(project, outside)
        )
        assert err["error"]["code"] == "path_not_allowed"
    assert list(outside.iterdir()) == []
    assert not (project / "stolen.md").exists()


async def test_download_extra_roots_are_allowed(doc_app, db_path, project, outside):
    add_vault(db_path, VAULT)
    shell = _shell(asgi(doc_app), project, upload_roots=(outside,))
    async with session(shell) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        await s.ok("download", vault=VAULT, id=doc_id, path=str(outside))
    assert (outside / "設定.md").read_bytes() == CONTENT


async def test_download_rejects_symlink_escape(doc_app, db_path, project, outside):
    link = project / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:  # Windows 無權限建立 symlink
        pytest.skip(f"無法建立 symlink：{exc}")
    add_vault(db_path, VAULT)
    async with session(_shell(asgi(doc_app), project)) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        err = await s.err("download", vault=VAULT, id=doc_id, path="link/x.md")
        assert err["error"]["code"] == "path_not_allowed"
    assert list(outside.iterdir()) == []


def test_download_path_checks_realpath(project, outside, monkeypatch):
    """不依賴建 symlink 權限：父目錄 realpath 指到白名單外即拒絕（拿掉比對會紅）。"""
    import lore_vault.mcp.download as download_mod

    real = os.path.realpath
    target_dir = os.path.normcase(str(project / "out"))

    def fake_realpath(path, *args, **kwargs):
        if os.path.normcase(str(path)) == target_dir:
            return str(outside)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(download_mod.os.path, "realpath", fake_realpath)
    with pytest.raises(DownloadPathError) as info:
        resolve_download_path("out/x.md", [project], cwd=str(project), filename="f")
    assert info.value.code == "path_not_allowed"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("設定.md", "設定.md"),
        ("../evil.md", "document-abc"),
        ("a/b.md", "document-abc"),
        ("a\\b.md", "document-abc"),
        ("C:evil.md", "document-abc"),
        ("..", "document-abc"),
        ("", "document-abc"),
        ("bad\x01.md", "document-abc"),
        ("NUL.txt", "document-abc"),
        ("trailing.", "document-abc"),
        (None, "document-abc"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name, "doc:abc") == expected


async def test_download_rejects_note_id_and_too_large(doc_app, db_path, project):
    add_vault(db_path, VAULT)
    shell = _shell(asgi(doc_app), project, download_max_bytes=len(CONTENT) - 1)
    async with session(shell) as client:
        s = Session(client)
        doc_id = (await _upload(s))["document_id"]
        err = await s.err("download", vault=VAULT, id="note-1")
        assert err["error"]["code"] == "invalid_request"
        err = await s.err("download", vault=VAULT, id=doc_id)
        assert err["error"]["code"] == "too_large" and "UI" in err["hint"]
    assert not (project / "設定.md").exists()


def _fake_download(content: bytes, sha: str, filename: str = "x.md"):
    """不理會 max_bytes、直接回內容的假服務（驗證殼端自己的保護）。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=content,
            headers={
                "content-type": "text/markdown",
                "content-disposition": f"attachment; filename*=UTF-8''{filename}",
                "x-lore-vault-sha256": sha,
            },
        )

    return httpx2.MockTransport(handler)


async def test_shell_enforces_size_limit_itself(project):
    big = b"x" * 100
    transport = _fake_download(big, hashlib.sha256(big).hexdigest())
    shell = _shell(transport, project, download_max_bytes=10)
    async with session(shell) as client:
        err = await Session(client).err("download", vault=VAULT, id="doc:a")
        assert err["error"]["code"] == "too_large"
    assert not (project / "x.md").exists()


async def test_hash_mismatch_is_not_written(project):
    transport = _fake_download(CONTENT, "0" * 64)
    async with session(_shell(transport, project)) as client:
        err = await Session(client).err("download", vault=VAULT, id="doc:a")
        assert err["error"]["code"] == "hash_mismatch"
    assert not (project / "x.md").exists()


async def test_unsafe_server_filename_falls_back(project):
    transport = _fake_download(
        CONTENT, hashlib.sha256(CONTENT).hexdigest(), filename="..%2Fevil.md"
    )
    async with session(_shell(transport, project)) as client:
        result = await Session(client).ok("download", vault=VAULT, id="doc:a")
    assert result["path"] == str(project / "document-a")
    assert (project / "document-a").read_bytes() == CONTENT
    assert not (project.parent / "evil.md").exists()
