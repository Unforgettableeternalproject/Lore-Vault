"""服務不可達時的降級矩陣（T-31）：讀取走本地快照、寫入直接失敗、4xx／3xx 不降級。"""

from __future__ import annotations

import json

import httpx2
import pytest

from lore_vault.storage import snapshot as storage_snapshot

from .conftest import (
    Session,
    add_vault,
    asgi,
    asgi_no_raise,
    failing,
    make_shell,
    session,
    status_transport,
)

pytestmark = pytest.mark.anyio

VAULT_A = "github.com/o/a"
VAULT_B = "github.com/o/b"
FOLDER_VAULT = "folder/alpha"

# 每次呼叫產生新的 transport（client 關閉時會一併關掉 transport）
UNREACHABLE = {
    "connect_error": lambda: failing(
        lambda r: httpx2.ConnectError("refused", request=r)
    ),
    "read_timeout": lambda: failing(
        lambda r: httpx2.ReadTimeout("timed out", request=r)
    ),
    "http_503": lambda: status_transport(503, "<html>origin down</html>"),
    "http_502": lambda: status_transport(502, ""),
    "http_504": lambda: status_transport(504, "gateway timeout"),
    "http_530_cf_tunnel": lambda: status_transport(530, "<html>error 1033</html>"),
    "http_522_cf_origin_timeout": lambda: status_transport(522, ""),
}


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "Alpha"
    path.mkdir()
    return path


@pytest.fixture
async def seeded(app, db_path, snapshot_dir):
    """兩個 vault 各一則 note，加一個 folder vault，拉好快照；回傳 {vault: note}。"""
    add_vault(db_path, VAULT_A, aliases=["github.com/o/a-old"])
    add_vault(db_path, VAULT_B)
    add_vault(db_path, FOLDER_VAULT, display="Alpha")
    shell = make_shell(asgi(app), snapshot_dir)
    notes = {}
    async with session(shell) as client:
        s = Session(client)
        notes[VAULT_A] = await s.ok(
            "write",
            vault=VAULT_A,
            title="記憶快照 A",
            body="甲專案的記憶內容",
            author="Minka",
        )
        notes[VAULT_B] = await s.ok(
            "write", vault=VAULT_B, title="記憶快照 B", body="乙專案的記憶內容"
        )
        assert await shell.refresh_snapshot() is not None
    return notes


@pytest.mark.parametrize("cause", sorted(UNREACHABLE))
async def test_reads_degrade_writes_fail(seeded, snapshot_dir, project, cause):
    shell = make_shell(UNREACHABLE[cause](), snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        recalled = await s.ok("recall", vault=VAULT_A, query="記憶")
        assert recalled["degraded"] is True
        assert recalled["degraded_reason"] == "service_unreachable"
        assert recalled["degraded_detail"]
        assert recalled["mode"] == "lexical"
        assert recalled["snapshot"]["generated_at"]
        assert [i["id"] for i in recalled["items"]] == [seeded[VAULT_A]["id"]]

        got = await s.ok("get", vault=VAULT_A, ids=[seeded[VAULT_A]["id"]])
        assert got["degraded"] is True
        assert got["items"][0]["body"] == "甲專案的記憶內容"
        # 快照帶作者欄位（A22）
        assert got["items"][0]["author"] == "Minka"
        assert recalled["items"][0]["author"] == "Minka"

        listed = await s.ok("list", vault=VAULT_A)
        assert listed["degraded"] is True
        assert [i["id"] for i in listed["items"]] == [seeded[VAULT_A]["id"]]

        resolved = await s.ok("vault_resolve", cwd=str(project))
        assert resolved["degraded"] is True
        assert resolved["key"] == FOLDER_VAULT
        assert resolved["created"] is False

        # 寫入：明確失敗、不排佇列
        err = await s.err("write", vault=VAULT_A, title="t", body="b")
        assert err["error"]["code"] == "service_unreachable"
        assert "不會排入離線佇列" in err["error"]["message"]
        err = await s.err(
            "update",
            vault=VAULT_A,
            id=seeded[VAULT_A]["id"],
            expected_updated=seeded[VAULT_A]["updated"],
            title="x",
        )
        assert err["error"]["code"] == "service_unreachable"

        status = await s.ok("status")
        assert status["ok"] is False
        assert status["degraded_reason"] == "service_unreachable"
        assert status["service"]["reachable"] is False
        assert status["shell"]["ok"] is True  # 快照剛拉、schema 一致


async def test_create_vault_fails_when_unreachable(seeded, snapshot_dir, tmp_path):
    other = tmp_path / "Beta"
    other.mkdir()
    shell = make_shell(UNREACHABLE["connect_error"](), snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        err = await s.err("vault_resolve", cwd=str(other), create=True)
        assert err["error"]["code"] == "service_unreachable"
        err = await s.err("vault_resolve", cwd=str(other))
        assert err["error"]["code"] == "unknown_vault"
        assert err["error"]["degraded"] is True


async def test_degraded_reads_keep_vault_scope(seeded, snapshot_dir):
    shell = make_shell(UNREACHABLE["connect_error"](), snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        # B 的 note 內容同樣含「記憶」，但只查 A
        recalled = await s.ok("recall", vault=VAULT_A, query="記憶 乙專案")
        assert {i["vault"] for i in recalled["items"]} == {VAULT_A}
        got = await s.ok("get", vault=VAULT_A, ids=[seeded[VAULT_B]["id"]])
        assert got["items"] == []
        assert got["missing"] == [seeded[VAULT_B]["id"]]
        listed = await s.ok("list", vault=VAULT_B)
        assert {i["vault"] for i in listed["items"]} == {VAULT_B}
        # 別名在快照裡也解析得到
        via_alias = await s.ok("list", vault="github.com/o/a-old")
        assert [i["id"] for i in via_alias["items"]] == [seeded[VAULT_A]["id"]]
        # 跨 vault 必須明示
        both = await s.ok("recall", vault="*", query="記憶")
        assert {i["vault"] for i in both["items"]} == {VAULT_A, VAULT_B}
        err = await s.err("recall", vault="", query="記憶")
        assert err["error"]["code"] == "vault_required"
        assert err["error"]["degraded"] is True
        err = await s.err("list", vault="github.com/o/none")
        assert err["error"]["code"] == "unknown_vault"


async def test_real_service_500_is_reported(app, seeded, snapshot_dir, monkeypatch):
    """真正的 create_app 回 500 storage_error：錯誤碼原樣回報，不讀快照。"""
    from lore_vault.api import routes
    from lore_vault.storage.errors import SchemaVersionError

    def broken(*args, **kwargs):
        raise SchemaVersionError("資料庫 schema 版本 9 比程式預期新")

    monkeypatch.setattr(routes, "recall_service", broken)
    shell = make_shell(asgi_no_raise(app), snapshot_dir)
    async with session(shell) as client:
        err = await Session(client).err("recall", vault=VAULT_A, query="記憶")
    assert err["http_status"] == 500
    assert err["error"]["code"] == "storage_error"
    assert "schema 版本 9" in err["error"]["message"]


async def test_no_snapshot_configured(app):
    shell = make_shell(UNREACHABLE["connect_error"](), None)
    async with session(shell) as client:
        err = await Session(client).err("recall", vault=VAULT_A, query="x")
    assert err["error"]["code"] == "service_unreachable"
    assert "mcp.snapshot_dir" in err["error"]["message"]


async def test_snapshot_never_pulled(snapshot_dir):
    shell = make_shell(UNREACHABLE["connect_error"](), snapshot_dir)
    async with session(shell) as client:
        err = await Session(client).err("recall", vault=VAULT_A, query="x")
    assert err["error"]["code"] == "service_unreachable"
    assert "尚未拉取快照" in err["error"]["message"]


async def test_snapshot_schema_mismatch_refused(seeded, snapshot_dir):
    manifest_path = snapshot_dir / storage_snapshot.MANIFEST_NAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    shell = make_shell(UNREACHABLE["connect_error"](), snapshot_dir)
    async with session(shell) as client:
        err = await Session(client).err("recall", vault=VAULT_A, query="記憶")
    assert "schema 版本 999" in err["error"]["message"]


@pytest.mark.parametrize(
    ("transport", "status"),
    [
        (
            status_transport(302, "", {"location": "https://x.cloudflareaccess.com"}),
            302,
        ),
        (
            status_transport(401, {"error": {"code": "unauthorized", "message": "x"}}),
            401,
        ),
        (status_transport(403, "<html>Forbidden</html>"), 403),
        (status_transport(404, "not found"), 404),
        # 500 是服務端明確的錯誤（如 storage_error），不可被降級掩蓋
        (
            status_transport(
                500, {"error": {"code": "storage_error", "message": "schema 不符"}}
            ),
            500,
        ),
        (status_transport(500, "<html>Internal Server Error</html>"), 500),
    ],
)
async def test_client_errors_do_not_degrade(seeded, snapshot_dir, transport, status):
    shell = make_shell(transport, snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        for name, args in (
            ("recall", {"vault": VAULT_A, "query": "記憶"}),
            ("get", {"vault": VAULT_A, "ids": ["x"]}),
            ("list", {"vault": VAULT_A}),
        ):
            err = await s.err(name, **args)
            assert err["http_status"] == status
            assert "degraded" not in err["error"]
        # status 也不把 500 當成不可達
        err = await s.err("status")
        assert err["http_status"] == status
