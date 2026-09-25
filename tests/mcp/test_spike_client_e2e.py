"""客戶端 × 真正的服務端點（`create_app`）契約測試：不開網路埠。

- hook 端 `urllib` 客戶端打 `FakeService`，由它把請求轉給 ASGI app
  （`POST /v1/episodes`）
- MCP 殼經 ASGITransport 拉 `GET /v1/concepts/export`，
  寫出的快照逐位元組等於服務端 export
"""

from __future__ import annotations

import anyio
import httpx2
import pytest

from lore_vault.hooks import concept_snapshot, spool
from lore_vault.hooks.client_env import ClientSettings, Secret
from lore_vault.hooks.service import request_json

from ..fake_service import FakeService
from .conftest import BASE_URL, TOKEN, asgi, make_shell


def _forward_to(app):
    """FakeService handler：把收到的 HTTP 請求原樣轉給 ASGI app。"""

    def handler(method, path, headers, body):
        async def call():
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.request(
                    method,
                    path,
                    json=body,
                    headers={"authorization": headers.get("authorization", "")},
                )
                return resp.status_code, resp.content

        status, content = anyio.run(call)
        return status, content, {}

    return handler


def _episode(n: int, **extra) -> dict:
    data = {
        "prompt_id": f"p-{n}",
        "turn_index": n,
        "session_id": "s-e2e",
        "agent": "claude-code",
        "origin": "human",
        "started_at": f"2026-09-20T02:0{n}:00.000Z",
        "ended_at": f"2026-09-20T02:0{n}:30.000Z",
        "cwd": ["C:/src/demo"],
        "repo": "demo",
        "repo_root": "C:/src/demo",
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "u",
        "assistant_text": "a",
        "injected": [],
        "tool_sequence": [{"name": "Edit", "count": 1}],
        "tool_calls_total": 1,
        "mcp_tools": [],
        "skills": [],
        "files_edited": ["a.py"],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
    }
    data.update(extra)
    return data


def test_spool_push_against_real_endpoint(app, tmp_path):
    spool_dir = tmp_path / "spool"
    good = [_episode(1), _episode(2)]
    bad = _episode(3, started_at="2026-09-20 02:03:00")  # 沒有時區 → invalid
    for ep in (*good, bad):
        spool.write_pending(
            spool_dir,
            spool.wire_episode(ep, machine="desk-a", vault="folder/demo-e2e"),
        )

    with FakeService(_forward_to(app)) as svc:
        settings = ClientSettings(env_file=None, url=svc.url, token=Secret(TOKEN))
        result = spool.push_all(spool_dir, settings)
        assert (result.accepted, result.rejected, result.kept) == (2, 1, 0)
        assert spool.spool_stats(spool_dir).rejected_by_status == {"invalid": 1}

        # 重播同一輪（例如 push 回應遺失後重送）→ duplicate，視同成功
        spool.write_pending(
            spool_dir,
            spool.wire_episode(good[0], machine="desk-a", vault="folder/demo-e2e"),
        )
        again = spool.push_all(spool_dir, settings)
        assert (again.duplicate, again.kept) == (1, 0)

        page = request_json(
            settings,
            "GET",
            "/v1/episodes",
            timeout=5,
            query={"vault": "folder/demo-e2e"},
        )
    items = page["items"]
    assert [i["prompt_id"] for i in items] == ["p-1", "p-2"]
    frozen = {(i["machine"], i["vault"]) for i in items}
    assert frozen == {("desk-a", "folder/demo-e2e")}


@pytest.mark.anyio
async def test_concept_snapshot_matches_service_export(app, tmp_path):
    concepts = [
        {
            "id": f"c-{i}",
            "statement": f"statement {i}",
            "kind": "project-fact",
            "scope": None,
            "anchors": ["src/a.py", f"sym{i}"],
            "surprisal": 0.9,
        }
        for i in range(3)
    ]
    path = tmp_path / "snap" / "concepts.json"
    shell = make_shell(asgi(app), concept_snapshot_path=path)
    try:
        applied = await shell.client.post(
            "/v1/concepts", {"vault": "*", "concepts": concepts}
        )
        assert applied["applied"] is True
        manifest = await shell.refresh_concepts()
        assert manifest is not None and manifest.concepts == 3

        async with httpx2.AsyncClient(
            transport=asgi(app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            export = await client.get("/v1/concepts/export")
        assert path.read_bytes() == export.content
        assert export.headers["etag"].strip('"') == manifest.sha256

        again = await shell.refresh_concepts()  # 未變 → 304
        assert again is not None and again.fetched_at == manifest.fetched_at
    finally:
        await shell.aclose()
    loaded, degraded = concept_snapshot.load_for_injection(path)
    assert degraded is None and [c["id"] for c in loaded] == ["c-0", "c-1", "c-2"]


def _spike_transcript(tmp_path, repo, turns: int):
    """與 Claude Code transcript 同形的記錄（欄位名同 spike 測試 fixture）。"""
    import json

    records = []
    for i in range(turns):
        records.append(
            {
                "type": "user",
                "promptId": f"p{i}",
                "cwd": str(repo),
                "gitBranch": "main",
                "sessionId": "sess",
                "version": "2.1.216",
                "timestamp": f"2026-07-25T00:0{i}:00.000Z",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": f"問題 {i}"},
            }
        )
        records.append(
            {
                "type": "assistant",
                "cwd": str(repo),
                "gitBranch": "main",
                "sessionId": "sess",
                "version": "2.1.216",
                "timestamp": f"2026-07-25T00:0{i}:30.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "x"},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": str(repo / "src" / "a.py"),
                                "old_string": "def foo(): pass",
                                "new_string": "def foo_bar(): return 1",
                            },
                        },
                        {"type": "text", "text": "好"},
                    ],
                },
            }
        )
    path = tmp_path / "sess.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_real_stop_hook_output_is_accepted_by_service(app, tmp_path, monkeypatch):
    """真正上線的路徑：transcript → `hook_stop.sync()`（build_episode）→ spool →
    `POST /v1/episodes`。spike 產出的 episode 形狀若與服務端 schema 不合，
    這裡會變 invalid。"""
    import sys
    from pathlib import Path

    spike = Path(__file__).resolve().parents[2] / "agent_memory_spike"
    monkeypatch.syspath_prepend(str(spike))
    import hook_stop

    repo = tmp_path / "Demo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    ep_dir = tmp_path / "data" / "episodes"
    written, _ = hook_stop.sync(_spike_transcript(tmp_path, repo, 4), ep_dir, "sess")
    assert written == 3

    spool_dir = hook_stop.spool_dir_for(ep_dir)
    with FakeService(_forward_to(app)) as svc:
        settings = ClientSettings(env_file=None, url=svc.url, token=Secret(TOKEN))
        result = spool.push_all(spool_dir, settings)
        page = request_json(
            settings, "GET", "/v1/episodes", timeout=5, query={"vault": "*"}
        )
    assert (result.accepted, result.rejected, result.kept) == (written, 0, 0)
    assert spool.spool_stats(spool_dir).pending == 0
    items = page["items"]
    assert len(items) == written
    assert {i["vault"] for i in items} == {"folder/demo"}
    assert {i["machine"] for i in items} == {hook_stop.current_machine()}
    assert "hook_stop" in sys.modules
