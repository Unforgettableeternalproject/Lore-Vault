"""端到端：MCP 工具呼叫 → 殼 → httpx2 ASGI → 真正的 create_app（T-29／T-30）。"""

from __future__ import annotations

import pytest

from lore_vault.binding import folder_key
from lore_vault.config import Secret
from lore_vault.schema import DEFAULT_PRINCIPAL

from .conftest import (
    CF_ID,
    CF_SECRET,
    TOKEN,
    CfEdge,
    HeaderRecorder,
    Session,
    add_vault,
    asgi,
    make_shell,
    session,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "MyProj"
    path.mkdir()
    return path


async def test_local_full_flow(app, project):
    recorder = HeaderRecorder(app)
    async with session(make_shell(asgi(recorder))) as client:
        s = Session(client)

        # 未知 vault：明確指引，不自動建立
        err = await s.err("vault_resolve", cwd=str(project))
        assert err["error"]["code"] == "unknown_vault"
        assert "create=True" in err["hint"]
        assert err["error"]["binding"]["key"] == folder_key("MyProj")

        created = await s.ok("vault_resolve", cwd=str(project), create=True)
        assert created["created"] is True
        assert created["key"] == "folder/myproj"
        assert created["display"] == "MyProj"
        again = await s.ok("vault_resolve", cwd=str(project), create=True)
        assert again["created"] is False
        assert again["binding"]["source"] == "folder"
        vault = again["key"]

        wrote = await s.ok(
            "write",
            vault=vault,
            title="快照降級設計",
            body="服務不可達時讀本地快照",
            author="Minka",
        )
        # A22：author 由殼轉送；principal 由服務依憑證判定
        assert (wrote["author"], wrote["principal"]) == ("Minka", DEFAULT_PRINCIPAL)
        recalled = await s.ok("recall", vault=vault, query="快照")
        assert [i["id"] for i in recalled["items"]] == [wrote["id"]]
        assert "body" not in recalled["items"][0]
        # embedder 不可用 → 服務自己的降級（與殼的 service_unreachable 不同）
        assert recalled["degraded_reason"] != "service_unreachable"

        got = await s.ok("get", vault=vault, ids=[wrote["id"], "nope"])
        assert got["items"][0]["body"] == "服務不可達時讀本地快照"
        assert got["missing"] == ["nope"]

        listed = await s.ok("list", vault=vault)
        assert [i["id"] for i in listed["items"]] == [wrote["id"]]

        updated = await s.ok(
            "update",
            vault=vault,
            id=wrote["id"],
            expected_updated=wrote["updated"],
            title="快照降級設計（修訂）",
            author="Novia",
        )
        assert (updated["author"], updated["updated_by"]) == ("Minka", "Novia")
        # 用舊版本再改一次 → 版本衝突，附目前版本讓 agent 重試
        conflict = await s.err(
            "update",
            vault=vault,
            id=wrote["id"],
            expected_updated=wrote["updated"],
            body="新內容",
        )
        assert conflict["error"]["code"] == "version_conflict"
        assert conflict["http_status"] == 409
        assert conflict["error"]["current"]["updated"] == updated["updated"]
        assert "current.updated" in conflict["hint"]
        retried = await s.ok(
            "update",
            vault=vault,
            id=wrote["id"],
            expected_updated=conflict["error"]["current"]["updated"],
            body="新內容",
        )
        assert retried["id"] == wrote["id"]

        # supersedes：設定後以空字串清除
        newer = await s.ok(
            "write", vault=vault, title="取代版", body="新版", supersedes=wrote["id"]
        )
        got = await s.ok("get", vault=vault, ids=[newer["id"]])
        assert got["items"][0]["supersedes"] == wrote["id"]
        cleared = await s.ok(
            "update",
            vault=vault,
            id=newer["id"],
            expected_updated=newer["updated"],
            supersedes="",
        )
        got = await s.ok("get", vault=vault, ids=[newer["id"]])
        assert got["items"][0]["supersedes"] is None
        assert got["items"][0]["updated"] == cleared["updated"]

        status = await s.ok("status", vault=vault)
        assert status["vault"]["key"] == vault
        assert status["shell"]["base_url"] == "http://lore.test"

    # 本機直連：帶 bearer、不帶 CF header
    assert recorder.seen
    for headers in recorder.seen:
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert "cf-access-client-id" not in headers
        assert "cf-access-client-secret" not in headers


async def test_vault_errors_are_explained(app, db_path):
    add_vault(db_path, "github.com/o/a")
    async with session(make_shell(asgi(app))) as client:
        s = Session(client)
        err = await s.err("recall", vault="github.com/o/none", query="x")
        assert err["error"]["code"] == "unknown_vault"
        assert "vault_resolve" in err["hint"]
        err = await s.err("write", vault="*", title="t", body="b")
        assert err["error"]["code"] == "vault_required"
        assert err["http_status"] == 400


async def test_via_cloudflare_access(app, db_path, snapshot_dir):
    add_vault(db_path, "github.com/o/a")
    edge = CfEdge(app)
    with_cf = make_shell(
        asgi(edge), snapshot_dir, cf_access=(Secret(CF_ID), Secret(CF_SECRET))
    )
    async with session(with_cf) as client:
        s = Session(client)
        wrote = await s.ok("write", vault="github.com/o/a", title="經 CF", body="內容")
        found = await s.ok("recall", vault="github.com/o/a", query="CF")
        assert found["items"][0]["id"] == wrote["id"]
        assert await with_cf.refresh_snapshot() is not None
    assert all(h["cf-access-client-id"] == CF_ID for h in edge.seen)
    assert all(h["authorization"] == f"Bearer {TOKEN}" for h in edge.seen)

    # 沒設 CF token：403 是設定錯誤，直接報錯；即使有快照也不降級
    without_cf = make_shell(asgi(edge), snapshot_dir)
    async with session(without_cf) as client:
        err = await Session(client).err("recall", vault="github.com/o/a", query="CF")
    assert err["http_status"] == 403
    assert "CF_ACCESS_CLIENT_ID" in err["error"]["message"]
    assert "degraded" not in err["error"]
    text = str(err)
    assert CF_SECRET not in text and TOKEN not in text


async def test_unauthorized_is_not_degraded(app, db_path, snapshot_dir):
    add_vault(db_path, "github.com/o/a")
    good = make_shell(asgi(app), snapshot_dir)
    assert await good.refresh_snapshot() is not None
    await good.aclose()

    wrong = "wrong-token-000000000000"
    bad = make_shell(asgi(app), snapshot_dir, token=Secret(wrong))
    async with session(bad) as client:
        s = Session(client)
        for name, args in (
            ("recall", {"vault": "github.com/o/a", "query": "x"}),
            ("get", {"vault": "github.com/o/a", "ids": ["x"]}),
            ("list", {"vault": "github.com/o/a"}),
            ("write", {"vault": "github.com/o/a", "title": "t", "body": "b"}),
        ):
            err = await s.err(name, **args)
            assert err["http_status"] == 401, name
            assert err["error"]["code"] == "unauthorized"
            assert "LORE_VAULT_API_TOKEN" in err["error"]["message"]
            assert wrong not in str(err) and TOKEN not in str(err)
        # 拉快照遇到 401 也只記錯誤，不動既有快照
        assert await bad.refresh_snapshot() is None
        assert "401" in bad.last_pull_error
        assert wrong not in bad.last_pull_error
