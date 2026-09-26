"""A18 殼端（T-55、T-56）：「目前 space」由殼持有、不持久化，其他工具自動帶入。

- 新殼 `space(get)` = dev；`set` 後 recall／list／get 只看目前 space
- `set` 非法值 → 工具錯誤，不打服務
- lore 的 vault_resolve：必須帶 key、cwd 被忽略並註記；前綴不符由服務拒絕
- 殼送出的每個 `/v1/*` 請求都帶 space（含 vault_resolve 與建 vault）
- 降級：服務不可達＋殼在 lore，快照查詢不會回出 dev 的內容；反之亦然
"""

from __future__ import annotations

import json

import httpx2
import pytest

from .conftest import (
    Session,
    add_vault,
    asgi,
    failing,
    make_shell,
    session,
)

pytestmark = pytest.mark.anyio

DEV = "github.com/o/dev"
LORE = "lore/arc"
QUERY = "記憶 世界觀"


class BodyRecorder:
    """記錄每個 POST 的 JSON body 後原樣轉給服務。"""

    def __init__(self, app) -> None:
        self.app = app
        self.bodies: list[tuple[str, dict]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        raw = b"".join(chunks)
        self.bodies.append((scope["path"], json.loads(raw) if raw else {}))
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


@pytest.fixture
async def two_spaces(app, db_path, snapshot_dir):
    add_vault(db_path, DEV)
    shell = make_shell(asgi(app), snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        dev = await s.ok("write", vault=DEV, title="開發記憶", body="記憶 世界觀 dev")
        await s.ok("space", action="set", value="lore")
        created = await s.ok(
            "vault_resolve", key=LORE, create=True, display="Aeswir Arc"
        )
        assert created["created"] is True and created["space"] == "lore"
        lore = await s.ok("write", vault=LORE, title="世界觀設定", body="記憶 世界觀")
        assert await shell.refresh_snapshot() is not None
    return {"dev": dev["id"], "lore": lore["id"]}


async def test_new_shell_starts_in_dev(app):
    shell = make_shell(failing(lambda r: AssertionError("不應打服務")))
    async with session(shell) as client:
        s = Session(client)
        assert (await s.ok("space", action="get"))["space"] == "dev"
        assert (await s.ok("space", action="set", value="personal"))[
            "space"
        ] == "personal"
        assert (await s.ok("space", action="get"))["space"] == "personal"
        err = await s.err("space", action="set", value="work")
        assert err["error"]["code"] == "invalid_space"
        err = await s.err("space", action="set")
        assert err["error"]["code"] == "space_required"
        err = await s.err("space", action="toggle")
        assert err["error"]["code"] == "invalid_request"
        # 失敗的切換不改狀態
        assert (await s.ok("space", action="get"))["space"] == "personal"
    # 新殼行程（不持久化）回到 dev
    assert make_shell(asgi(app)).space == "dev"


async def test_tools_only_see_current_space(app, two_spaces, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    async with session(shell) as client:
        s = Session(client)
        # dev（預設）
        found = await s.ok("recall", vault="*", query=QUERY)
        assert {i["id"] for i in found["items"]} == {two_spaces["dev"]}
        err = await s.err("list", vault=LORE)
        assert err["error"]["code"] == "unknown_vault"
        # 切到 lore
        await s.ok("space", action="set", value="lore")
        found = await s.ok("recall", vault="*", query=QUERY)
        assert {i["id"] for i in found["items"]} == {two_spaces["lore"]}
        got = await s.ok("get", vault=LORE, ids=[two_spaces["dev"]])
        assert got["missing"] == [two_spaces["dev"]]
        err = await s.err("list", vault=DEV)
        assert err["error"]["code"] == "unknown_vault"
        status = await s.ok("status", vault=LORE)
        assert status["vault"]["space"] == "lore"
        assert status["shell"]["space"] == "lore"


async def test_every_request_carries_current_space(app, db_path, tmp_path):
    add_vault(db_path, DEV)
    recorder = BodyRecorder(app)
    shell = make_shell(asgi(recorder))
    async with session(shell) as client:
        s = Session(client)
        await s.ok("recall", vault=DEV, query="x")
        await s.ok("list", vault=DEV)
        await s.ok("status")
        await s.ok("space", action="set", value="lore")
        await s.ok("vault_resolve", key="lore/new", create=True, display="n")
        # 顯式 space 只影響這一次
        await s.ok("vault_resolve", key="personal/diary", create=True, space="personal")
        assert (await s.ok("space", action="get"))["space"] == "lore"
    paths = [(p, b.get("space")) for p, b in recorder.bodies]
    assert paths == [
        ("/v1/recall", "dev"),
        ("/v1/list", "dev"),
        ("/v1/status", "dev"),
        ("/v1/vault_resolve", "lore"),
        ("/v1/vaults", "lore"),
        ("/v1/vault_resolve", "personal"),
        ("/v1/vaults", "personal"),
    ]


async def test_non_dev_vault_resolve_needs_key_and_ignores_cwd(app, tmp_path):
    shell = make_shell(asgi(app))
    async with session(shell) as client:
        s = Session(client)
        await s.ok("space", action="set", value="lore")
        err = await s.err("vault_resolve", create=True)
        assert err["error"]["code"] == "key_required"
        err = await s.err("vault_resolve", key="aeswir-arc", create=True)
        assert err["error"]["code"] == "space_key_prefix_required"
        made = await s.ok(
            "vault_resolve", key="lore/aeswir-arc", create=True, cwd=str(tmp_path)
        )
        assert made["cwd_ignored"] is True
        assert "binding" not in made
        again = await s.ok("vault_resolve", key="lore/aeswir-arc")
        assert again["created"] is False and again["key"] == "lore/aeswir-arc"
        # lore 不自動建 global
        err = await s.err("vault_resolve", key="lore/global")
        assert err["error"]["code"] == "unknown_vault"


async def test_degraded_reads_filter_by_current_space(two_spaces, snapshot_dir):
    """服務不可達＋殼切到 lore：快照查詢不會回出 dev 的內容（反之亦然）。"""
    down = make_shell(
        failing(lambda r: httpx2.ConnectError("refused", request=r)), snapshot_dir
    )
    async with session(down) as client:
        s = Session(client)
        found = await s.ok("recall", vault="*", query=QUERY)
        assert found["degraded"] is True
        assert {i["id"] for i in found["items"]} == {two_spaces["dev"]}

        await s.ok("space", action="set", value="lore")
        found = await s.ok("recall", vault="*", query=QUERY)
        assert found["degraded"] is True
        assert {i["id"] for i in found["items"]} == {two_spaces["lore"]}
        listed = await s.ok("list", vault="*")
        assert {i["id"] for i in listed["items"]} == {two_spaces["lore"]}
        got = await s.ok("get", vault=LORE, ids=[two_spaces["dev"]])
        assert got["missing"] == [two_spaces["dev"]]
        err = await s.err("list", vault=DEV)
        assert err["error"]["code"] == "unknown_vault"
        assert err["error"]["degraded"] is True
        resolved = await s.ok("vault_resolve", key=LORE)
        assert resolved["space"] == "lore" and resolved["degraded"] is True
