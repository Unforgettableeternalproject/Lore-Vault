"""快照條件式請求與服務端快取（T-31 裁決 1）：ETag／304、指紋失效條件、定期拉取。"""

from __future__ import annotations

import json
from datetime import timedelta

import anyio
import httpx2
import pytest

from lore_vault import notes as notes_service
from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.schema import Vault
from lore_vault.storage import enrichment, vectors
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.db import connect
from lore_vault.storage.notes import delete_note
from lore_vault.storage.timeutil import format_utc, parse_utc
from lore_vault.storage.vaults import upsert_vault

from .conftest import DIM, TOKEN, HeaderRecorder, add_vault, asgi, make_shell

pytestmark = pytest.mark.anyio

VAULT = "github.com/o/a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def seeded(db_path):
    add_vault(db_path, VAULT)
    conn = connect(db_path)
    try:
        ids = [
            notes_service.write(conn, VAULT, f"記憶 {i}", f"內容 {i}").note.id
            for i in range(3)
        ]
    finally:
        conn.close()
    return ids


def builds(app) -> int:
    return app.state.lore.snapshot_cache().builds


async def fetch(app, if_none_match: str | None = None) -> httpx2.Response:
    headers = dict(AUTH)
    if if_none_match is not None:
        headers["If-None-Match"] = if_none_match
    async with httpx2.AsyncClient(
        transport=asgi(app), base_url="http://lore.test"
    ) as client:
        return await client.get("/v1/snapshot", headers=headers)


# ── 服務端：ETag／304 ──


async def test_etag_and_304(app, seeded):
    first = await fetch(app)
    assert first.status_code == 200
    sha = first.headers[storage_snapshot.HEADER_SHA256]
    assert first.headers["etag"] == f'"{sha}"'

    for tag in (f'"{sha}"', f'W/"{sha}"', f'"other", "{sha}"', "*"):
        resp = await fetch(app, tag)
        assert resp.status_code == 304, tag
        assert resp.content == b""
        assert resp.headers[storage_snapshot.HEADER_SHA256] == sha
        assert resp.headers["etag"] == f'"{sha}"'

    stale = await fetch(app, '"0000"')
    assert stale.status_code == 200
    assert stale.content == first.content
    # 資料沒變：全程只建一次
    assert builds(app) == 1


def _mutations():
    def write(conn, ids):
        notes_service.write(conn, VAULT, "新的", "新內容")

    def update_title(conn, ids):
        note = notes_service.get(conn, VAULT, [ids[0]]).items[0]
        notes_service.update(conn, VAULT, ids[0], note["updated"], title="改標題")

    def summary_only(conn, ids):
        # 背景補摘要不推進 updated：「max(updated) + 筆數」會漏掉這種變動
        row = conn.execute(
            "SELECT seq, updated FROM notes WHERE id = ?", (ids[0],)
        ).fetchone()
        assert enrichment.write_summary_if_current(
            conn, row["seq"], row["updated"], "補上的摘要"
        )

    def delete_then_write(conn, ids):
        # 筆數不變
        delete_note(conn, VAULT, ids[2])
        notes_service.write(conn, VAULT, "替代", "替代內容")

    def add_alias(conn, ids):
        upsert_vault(
            conn, Vault(key=VAULT, display=VAULT, aliases=("github.com/o/old",))
        )

    def rename_display(conn, ids):
        upsert_vault(conn, Vault(key=VAULT, display="新顯示名"))

    return {
        "write": write,
        "update_title": update_title,
        "summary_only": summary_only,
        "delete_then_write": delete_then_write,
        "add_alias": add_alias,
        "rename_display": rename_display,
    }


@pytest.mark.parametrize("mutation", sorted(_mutations()))
async def test_cache_invalidates_on_snapshot_data_change(
    app, db_path, seeded, mutation
):
    first = await fetch(app)
    sha = first.headers[storage_snapshot.HEADER_SHA256]
    conn = connect(db_path)
    try:
        _mutations()[mutation](conn, seeded)
    finally:
        conn.close()
    # 帶舊 ETag：資料已變 → 重建並回 200 新內容
    second = await fetch(app, f'"{sha}"')
    assert second.status_code == 200
    assert second.headers[storage_snapshot.HEADER_SHA256] != sha
    assert builds(app) == 2
    # 之後沒再變就不重建
    third = await fetch(app, second.headers["etag"])
    assert third.status_code == 304
    assert builds(app) == 2


async def test_cache_ignores_data_outside_snapshot(app, db_path, seeded):
    await fetch(app)
    conn = connect(db_path)
    try:
        vectors.set_embedding(conn, VAULT, seeded[0], [1.0] * DIM, dim=DIM)
    finally:
        conn.close()
    await fetch(app)
    assert builds(app) == 1


async def test_cache_rebuilds_when_cached_file_missing(app, seeded):
    await fetch(app)
    cache = app.state.lore.snapshot_cache()
    for path in cache.cache_dir.iterdir():
        path.unlink()
    resp = await fetch(app)
    assert resp.status_code == 200
    assert builds(app) == 2


# ── 殼端：304 沿用舊快照並更新確認時間 ──


async def test_shell_uses_304_and_updates_checked_at(app, seeded, snapshot_dir):
    recorder = HeaderRecorder(app)
    shell = make_shell(asgi(recorder), snapshot_dir)
    first = await shell.refresh_snapshot()
    await anyio.sleep(0.01)
    second = await shell.refresh_snapshot()
    await shell.aclose()

    assert first is not None and second is not None
    snapshot_requests = [h for h in recorder.seen if "if-none-match" in h]
    assert len(snapshot_requests) == 1
    assert snapshot_requests[0]["if-none-match"] == f'"{first.sha256}"'
    # 沿用：同一份檔案、同一個 pulled_at；只有確認時間前進
    assert second.sha256 == first.sha256
    assert second.pulled_at == first.pulled_at
    assert second.generated_at == first.generated_at
    assert parse_utc(second.checked_at) > parse_utc(first.checked_at)
    assert storage_snapshot.read_manifest(snapshot_dir) == second
    assert builds(app) == 1


async def test_shell_redownloads_when_local_file_changed(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    first = await shell.refresh_snapshot()
    db = snapshot_dir / storage_snapshot.SNAPSHOT_DB_NAME
    db.write_bytes(db.read_bytes() + b"junk")
    await anyio.sleep(0.01)
    # 本地檔與 manifest 不符：不帶 ETag，重新下載完整快照
    second = await shell.refresh_snapshot()
    await shell.aclose()
    assert second is not None
    assert parse_utc(second.pulled_at) > parse_utc(first.pulled_at)
    assert storage_snapshot.file_sha256(db) == first.sha256


async def test_doctor_age_counts_from_last_check(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    await shell.refresh_snapshot()
    await shell.aclose()
    path = snapshot_dir / storage_snapshot.MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    # 資料兩天沒變（generated_at 很舊），但剛剛 304 確認過 → 不算過舊
    now = parse_utc(data["checked_at"])
    old = format_utc(now - timedelta(days=2))
    data["generated_at"] = data["pulled_at"] = old
    path.write_text(json.dumps(data), encoding="utf-8")
    report = default_registry().run(
        DoctorContext(settings={"snapshot_dir": str(snapshot_dir), "now": now}),
        categories=["snapshot"],
    )
    age = {o.name: o.result for o in report.outcomes}["snapshot.age"]
    assert age.status is Status.PASS
    # 確認時間也很舊 → fail
    data["checked_at"] = old
    path.write_text(json.dumps(data), encoding="utf-8")
    report = default_registry().run(
        DoctorContext(settings={"snapshot_dir": str(snapshot_dir), "now": now}),
        categories=["snapshot"],
    )
    age = {o.name: o.result for o in report.outcomes}["snapshot.age"]
    assert age.status is Status.FAIL


async def test_periodic_pull_repeats(app, seeded, snapshot_dir):
    from mcp.client.client import Client

    from lore_vault.mcp.server import build_server

    recorder = HeaderRecorder(app)
    shell = make_shell(
        asgi(recorder),
        snapshot_dir,
        snapshot_on_start=True,
        snapshot_interval=0.05,
    )

    async with Client(build_server(shell)):
        with anyio.fail_after(5):
            while len(recorder.seen) < 3:
                await anyio.sleep(0.02)
    # 第一次是完整下載，之後帶 ETag、服務端不重建
    assert "if-none-match" not in recorder.seen[0]
    assert all("if-none-match" in h for h in recorder.seen[1:])
    assert builds(app) == 1
