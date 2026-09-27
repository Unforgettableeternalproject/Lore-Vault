"""殼端 concept 快照（T-40）：`GET /v1/concepts/export` →
原子寫成 PreToolUse 讀的檔。"""

from __future__ import annotations

import hashlib
import json

import httpx2
import pytest

from lore_vault.hooks import concept_snapshot
from lore_vault.mcp.settings import load_shell_settings

from .conftest import TOKEN, failing, make_shell, status_transport

pytestmark = pytest.mark.anyio

CONCEPTS = [
    {"id": "c1", "surprisal": 0.9, "anchors": ["a.py", "foo"], "scope": None},
    {"id": "c2", "surprisal": 0.5, "anchors": ["b.py"], "scope": "demo"},
]
BODY = json.dumps(CONCEPTS, ensure_ascii=False, indent=2).encode("utf-8")
DIGEST = hashlib.sha256(BODY).hexdigest()


def export_transport(body: bytes = BODY, etag: str | None = DIGEST, calls=None):
    """模擬服務端：帶 If-None-Match 且相符回 304，否則回 200 + ETag。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/concepts/export"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if calls is not None:
            calls.append(request.headers.get("if-none-match"))
        headers = {"ETag": f'"{etag}"'} if etag else {}
        if etag and request.headers.get("if-none-match") == f'"{etag}"':
            return httpx2.Response(304, headers=headers)
        return httpx2.Response(200, content=body, headers=headers)

    return httpx2.MockTransport(handler)


def test_default_path_is_inside_snapshot_dir(tmp_path):
    env = {"LORE_VAULT_API_TOKEN": TOKEN}
    assert load_shell_settings(environ=env).concept_snapshot_path is None
    with_dir = load_shell_settings(
        environ={**env, "LORE_VAULT_MCP_SNAPSHOT_DIR": str(tmp_path)}
    )
    # 刻意不預設成 spike 管線的 ~/.lore-vault/concepts.json（快照在 snapshot/ 底下）
    assert with_dir.concept_snapshot_path == tmp_path / "concepts.json"
    explicit = load_shell_settings(
        environ={
            **env,
            "LORE_VAULT_MCP_CONCEPT_SNAPSHOT_PATH": str(tmp_path / "x" / "c.json"),
        }
    )
    assert explicit.concept_snapshot_path == tmp_path / "x" / "c.json"


async def test_pull_installs_then_uses_etag(tmp_path):
    path = tmp_path / "concepts.json"
    calls: list[str | None] = []
    shell = make_shell(export_transport(calls=calls), concept_snapshot_path=path)
    try:
        first = await shell.refresh_concepts()
        assert first is not None and first.concepts == 2
        # 逐位元組＝服務端 export（同 concepts.json 格式）
        assert path.read_bytes() == BODY
        checked_before = concept_snapshot.read_manifest(path).checked_at

        second = await shell.refresh_concepts()
        assert second is not None and second.sha256 == DIGEST
        assert calls == [None, f'"{DIGEST}"']
        assert concept_snapshot.read_manifest(path).checked_at >= checked_before
        assert shell.last_concept_pull_error is None
    finally:
        await shell.aclose()


@pytest.mark.parametrize(
    "transport",
    [
        export_transport(body=b'{"not": "a list"}', etag=None),
        export_transport(body=b"[1, 2]", etag=None),
        export_transport(etag="0" * 64),  # 內容與 ETag 不符
        status_transport(500, {"error": {"code": "x", "message": "boom"}}),
        status_transport(503, "down"),
        failing(lambda req: httpx2.ConnectError("refused", request=req)),
    ],
)
async def test_failed_pull_keeps_previous_snapshot(tmp_path, transport):
    path = tmp_path / "concepts.json"
    concept_snapshot.install(path, BODY)
    before = concept_snapshot.read_manifest(path)
    shell = make_shell(transport, concept_snapshot_path=path)
    try:
        assert await shell.refresh_concepts() is None
        assert shell.last_concept_pull_error
        assert TOKEN not in shell.last_concept_pull_error
    finally:
        await shell.aclose()
    assert path.read_bytes() == BODY
    assert concept_snapshot.read_manifest(path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "concepts.json",
        "concepts.json.manifest.json",
    ]


async def test_not_configured_does_nothing(tmp_path):
    shell = make_shell(export_transport())
    try:
        assert await shell.refresh_concepts() is None
    finally:
        await shell.aclose()
    assert list(tmp_path.iterdir()) == []
