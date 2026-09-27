"""快照：服務端 `GET /v1/snapshot`、殼端原子替換、doctor 對帳（T-31）。"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import timedelta

import anyio
import httpx2
import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.doctor.command import main as doctor_main
from lore_vault.recall import recall
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage import vectors
from lore_vault.storage.db import connect, connect_readonly
from lore_vault.storage.migrate import SCHEMA_VERSION
from lore_vault.storage.timeutil import parse_utc

from .conftest import (
    DIM,
    TOKEN,
    Session,
    add_vault,
    asgi,
    make_shell,
    session,
)

pytestmark = pytest.mark.anyio

VAULT = "github.com/o/a"


@pytest.fixture
async def seeded(app, db_path):
    add_vault(db_path, VAULT)
    add_vault(db_path, "github.com/o/private")
    async with session(make_shell(asgi(app))) as client:
        s = Session(client)
        note = await s.ok("write", vault=VAULT, title="記憶系統", body="快照內容")
        await s.ok("write", vault="github.com/o/private", title="其他", body="x")
    conn = connect(db_path)
    try:
        vectors.set_embedding(
            conn, VAULT, note["id"], [1.0] * DIM, space="dev", dim=DIM
        )
    finally:
        conn.close()
    return note


def _files(snapshot_dir):
    return sorted(p.name for p in snapshot_dir.iterdir())


def _sha(snapshot_dir) -> str:
    return storage_snapshot.file_sha256(
        snapshot_dir / storage_snapshot.SNAPSHOT_DB_NAME
    )


# ── 服務端 ──


async def test_endpoint_requires_auth_and_returns_consistent_copy(
    app, seeded, tmp_path
):
    async with httpx2.AsyncClient(
        transport=asgi(app), base_url="http://lore.test"
    ) as client:
        denied = await client.get("/v1/snapshot")
        assert denied.status_code == 401
        resp = await client.get(
            "/v1/snapshot", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == storage_snapshot.MEDIA_TYPE
    headers = resp.headers
    assert int(headers[storage_snapshot.HEADER_SCHEMA_VERSION]) == SCHEMA_VERSION
    assert int(headers[storage_snapshot.HEADER_NOTES]) == 2
    parse_utc(headers[storage_snapshot.HEADER_GENERATED_AT])
    path = tmp_path / "copy.db"
    path.write_bytes(resp.content)
    assert storage_snapshot.file_sha256(path) == headers[storage_snapshot.HEADER_SHA256]

    conn = connect_readonly(path)
    try:
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("vaults", "notes", "note_fts", "note_embeddings", "episodes")
        }
        # 白名單：note、vault、FTS 有；向量、episode 不帶
        assert counts == {
            "vaults": 2,
            "notes": 2,
            "note_fts": 2,
            "note_embeddings": 0,
            "episodes": 0,
        }
        # 快照上 lexical 檢索可用（CJK 2 字詞）
        result = recall(conn, "記憶", VAULT, space="dev", mode="lexical")
        assert [i.id for i in result.items] == [seeded["id"]]
    finally:
        conn.close()


# ── 殼端拉取與原子替換 ──


async def test_pull_installs_snapshot_and_manifest(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    manifest = await shell.refresh_snapshot()
    await shell.aclose()
    assert manifest is not None
    assert _files(snapshot_dir) == [
        storage_snapshot.SNAPSHOT_DB_NAME,
        storage_snapshot.MANIFEST_NAME,
    ]
    on_disk = storage_snapshot.read_manifest(snapshot_dir)
    assert on_disk == manifest
    assert on_disk.schema_version == SCHEMA_VERSION
    assert on_disk.service_version == "0.1.1"
    assert on_disk.sha256 == _sha(snapshot_dir)


class _Tamper:
    """把真正服務的快照回應改壞後回傳（模擬傳輸不完整、header 不符、中途斷線）。"""

    def __init__(self, app, mode: str) -> None:
        self.inner = asgi(app)
        self.mode = mode

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.url.path != "/v1/snapshot":
            return response
        body = await response.aread()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        if self.mode == "truncate":
            return httpx2.Response(200, headers=headers, content=body[: len(body) // 2])
        if self.mode == "schema":
            headers[storage_snapshot.HEADER_SCHEMA_VERSION] = str(SCHEMA_VERSION + 1)
            return httpx2.Response(200, headers=headers, content=body)
        if self.mode == "drop":

            async def chunks():
                yield body[:1024]
                raise httpx2.ReadError("connection reset")

            return httpx2.Response(200, headers=headers, content=chunks())
        raise AssertionError(self.mode)

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.parametrize("mode", ["truncate", "schema", "drop"])
async def test_failed_pull_keeps_old_snapshot(app, seeded, snapshot_dir, mode):
    good = make_shell(asgi(app), snapshot_dir)
    assert await good.refresh_snapshot() is not None
    await good.aclose()
    before_sha = _sha(snapshot_dir)
    before_manifest = storage_snapshot.read_manifest(snapshot_dir)

    bad = make_shell(_Tamper(app, mode), snapshot_dir)
    assert await bad.refresh_snapshot() is None
    await bad.aclose()
    assert bad.last_pull_error
    # 沒有半檔、舊快照與 manifest 原封不動且仍可查
    assert _files(snapshot_dir) == [
        storage_snapshot.SNAPSHOT_DB_NAME,
        storage_snapshot.MANIFEST_NAME,
    ]
    assert _sha(snapshot_dir) == before_sha
    assert storage_snapshot.read_manifest(snapshot_dir) == before_manifest
    conn, _ = storage_snapshot.open_snapshot(snapshot_dir)
    try:
        assert recall(conn, "記憶", VAULT, space="dev", mode="lexical").items
    finally:
        conn.close()


async def test_replace_failure_keeps_old_snapshot(
    app, seeded, snapshot_dir, monkeypatch
):
    good = make_shell(asgi(app), snapshot_dir)
    assert await good.refresh_snapshot() is not None
    before_sha = _sha(snapshot_dir)

    def locked(src, dst):
        raise PermissionError("目標檔被其他程序開著")

    monkeypatch.setattr(storage_snapshot, "_REPLACE_DELAY", 0)
    monkeypatch.setattr(storage_snapshot.os, "replace", locked)
    assert await good.refresh_snapshot() is None
    monkeypatch.undo()
    await good.aclose()
    assert "PermissionError" in good.last_pull_error
    assert _files(snapshot_dir) == [
        storage_snapshot.SNAPSHOT_DB_NAME,
        storage_snapshot.MANIFEST_NAME,
    ]
    assert _sha(snapshot_dir) == before_sha


async def test_install_removes_partial_on_verification_failure(tmp_path):
    partial = tmp_path / "x.partial"
    partial.write_bytes(b"not a database")
    with pytest.raises(storage_snapshot.SnapshotError):
        storage_snapshot.install_snapshot(
            partial,
            tmp_path,
            generated_at="2026-09-26T00:00:00.000Z",
            schema_version=SCHEMA_VERSION,
            service_version="0.1.0",
            sha256=storage_snapshot.file_sha256(partial),
        )
    assert not partial.exists()
    assert not (tmp_path / storage_snapshot.SNAPSHOT_DB_NAME).exists()


async def test_startup_pull_runs_in_background(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir, snapshot_on_start=True)
    async with session(shell) as client:
        # 啟動拉取在背景跑，不擋工具
        await Session(client).ok("status")
        for _ in range(100):
            if (snapshot_dir / storage_snapshot.MANIFEST_NAME).exists():
                break
            await anyio.sleep(0.02)
        assert (snapshot_dir / storage_snapshot.MANIFEST_NAME).exists()


# ── doctor ──


def _run_snapshot_checks(snapshot_dir, **settings):
    report = default_registry().run(
        DoctorContext(settings={"snapshot_dir": str(snapshot_dir), **settings}),
        categories=["snapshot"],
    )
    return {o.name: o.result for o in report.outcomes}


async def test_doctor_passes_on_fresh_snapshot(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    await shell.refresh_snapshot()
    await shell.aclose()
    results = _run_snapshot_checks(snapshot_dir)
    assert {n: r.status for n, r in results.items()} == {
        "snapshot.schema_version": Status.PASS,
        "snapshot.age": Status.PASS,
    }


async def test_doctor_fails_when_snapshot_too_old(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    manifest = await shell.refresh_snapshot()
    await shell.aclose()
    later = parse_utc(manifest.generated_at) + timedelta(hours=25)
    results = _run_snapshot_checks(snapshot_dir, now=later)
    assert results["snapshot.age"].status is Status.FAIL
    # 門檻放寬就回到 pass：證明紅燈來自年齡
    results = _run_snapshot_checks(snapshot_dir, now=later, snapshot_max_age_hours=48)
    assert results["snapshot.age"].status is Status.PASS

    out = io.StringIO()
    code = doctor_main(
        [
            "--category",
            "snapshot",
            "--snapshot-dir",
            str(snapshot_dir),
            "--snapshot-max-age-hours",
            "0.0000001",
        ],
        stdout=out,
    )
    assert code == 1
    assert "snapshot.age" in out.getvalue()


async def test_doctor_fails_on_schema_mismatch(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    await shell.refresh_snapshot()
    await shell.aclose()
    manifest_path = snapshot_dir / storage_snapshot.MANIFEST_NAME
    original = manifest_path.read_text(encoding="utf-8")

    # manifest 宣告的版本不符
    data = json.loads(original)
    data["schema_version"] = SCHEMA_VERSION + 1
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        _run_snapshot_checks(snapshot_dir)["snapshot.schema_version"].status
        is Status.FAIL
    )

    # 檔案本身的 user_version 不符（manifest sha 同步更新，排除 sha 檢查的干擾）
    manifest_path.write_text(original, encoding="utf-8")
    db = snapshot_dir / storage_snapshot.SNAPSHOT_DB_NAME
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    data = json.loads(original)
    data["sha256"] = storage_snapshot.file_sha256(db)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    result = _run_snapshot_checks(snapshot_dir)["snapshot.schema_version"]
    assert result.status is Status.FAIL
    assert result.counts["file_schema_version"] == SCHEMA_VERSION + 1


async def test_doctor_fails_on_sha_mismatch(app, seeded, snapshot_dir):
    shell = make_shell(asgi(app), snapshot_dir)
    await shell.refresh_snapshot()
    await shell.aclose()
    manifest_path = snapshot_dir / storage_snapshot.MANIFEST_NAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    result = _run_snapshot_checks(snapshot_dir)["snapshot.schema_version"]
    assert result.status is Status.FAIL
    assert "sha256" in result.summary


async def test_doctor_fails_when_never_pulled(snapshot_dir):
    results = _run_snapshot_checks(snapshot_dir)
    assert all(r.status is Status.FAIL for r in results.values())
    report = default_registry().run(DoctorContext(), categories=["snapshot"])
    assert all(o.result.status is Status.SKIPPED for o in report.outcomes)
