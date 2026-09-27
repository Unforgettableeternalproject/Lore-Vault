"""階段 8 spike 接入端點：episode 收料／讀取、concept 匯出／寫入、注入 side-car。"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from lore_vault.api import spike as spike_api
from lore_vault.schema import Concept
from lore_vault.storage import records
from lore_vault.storage.db import connect

from .conftest import create_vault


def episode(**overrides) -> dict:
    data = {
        "prompt_id": "p-1",
        "turn_index": 0,
        "session_id": "s-1",
        "agent": "claude-code",
        "origin": "human",
        "machine": "desktop-a",
        "started_at": "2026-09-01T02:00:00.000Z",
        "ended_at": "2026-09-01T02:05:00.000Z",
        "cwd": ["C:/repo"],
        "repo": "Repo-X",
        "repo_root": "C:/repo",
        "git_branch": ["main"],
        "cc_version": "2.0.0",
        "user_text": "u",
        "assistant_text": "a",
        "tool_sequence": [{"name": "Edit", "count": 1}],
        "tool_calls_total": 1,
        "mcp_tools": [],
        "skills": [],
        "files_edited": [],
        "files_read": [],
        "symbols_edited": [],
        "thinking_blocks": 0,
        "vault": "github.com/owner/repo-x",
    }
    data.update(overrides)
    return data


def spike_concept(cid: str, **overrides) -> dict:
    """spike distill.ingest 寫出的形狀（14 個鍵，順序同 spike；無 usability）。"""
    data = {
        "id": cid,
        "statement": f"陳述 {cid}",
        "kind": "project-fact",
        "scope": "Repo-X",
        "cue": f"cue {cid}",
        "probe": f"probe {cid}",
        "why": "why",
        "source_candidate": "cand-000",
        "from_signal": True,
        "source_turns": [["p-1", 0]],
        "source_files": ["src/a.py"],
        "anchors": ["src/a.py", "func_a"],
        "surprisal": None,
        "probe_result": None,
    }
    data.update(overrides)
    return data


def _db(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── POST /v1/episodes ───────────────────────────────────────────────


def test_episode_push_auto_creates_vault_and_is_idempotent(client, db_path):
    resp = client.post("/v1/episodes", json={"episodes": [episode()]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 1 and body["duplicates"] == 0
    assert body["created_vaults"] == ["github.com/owner/repo-x"]
    assert body["results"] == [
        {
            "index": 0,
            "key": ["s-1", "p-1", 0],
            "vault": "github.com/owner/repo-x",
            "status": "accepted",
        }
    ]
    again = client.post("/v1/episodes", json={"episodes": [episode()]}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 1
    assert again["created_vaults"] == []
    assert again["results"][0]["status"] == "duplicate"

    conn = _db(db_path)
    try:
        row = conn.execute(
            "SELECT display, kind, origin, origin_detail FROM vaults"
        ).fetchone()
    finally:
        conn.close()
    assert (row["display"], row["kind"], row["origin"]) == ("Repo-X", "repo", "episode")
    detail = json.loads(row["origin_detail"])
    assert detail["machine"] == "desktop-a" and detail["repo"] == "Repo-X"


def test_episode_conflict_is_reported_not_overwritten(client):
    client.post("/v1/episodes", json={"episodes": [episode()]})
    resp = client.post(
        "/v1/episodes", json={"episodes": [episode(user_text="被改過"), episode()]}
    ).json()
    assert [r["status"] for r in resp["results"]] == ["conflict", "duplicate"]
    assert resp["conflicts"] == 1
    items = client.get("/v1/episodes", params={"vault": "*"}).json()["items"]
    assert [i["user_text"] for i in items] == ["u"]


def test_episode_invalid_items_do_not_block_batch(client, db_path):
    bad_schema = episode(session_id="s-2")
    bad_schema["unknown_field"] = 1
    no_vault = episode(session_id="s-3")
    del no_vault["vault"]
    star = episode(session_id="s-4", vault="*")
    resp = client.post(
        "/v1/episodes",
        json={"episodes": [bad_schema, no_vault, star, "not-a-dict", episode()]},
    ).json()
    assert [r["status"] for r in resp["results"]] == [
        "invalid",
        "invalid",
        "invalid",
        "invalid",
        "accepted",
    ]
    assert resp["results"][3]["key"] is None
    assert resp["invalid"] == 4 and resp["accepted"] == 1
    conn = _db(db_path)
    try:
        # 失敗的筆不留下自動建立的 vault
        keys = [r[0] for r in conn.execute("SELECT key FROM vaults")]
    finally:
        conn.close()
    assert keys == ["github.com/owner/repo-x"]


def test_episode_conflict_rolls_back_auto_created_vault(client, db_path):
    client.post("/v1/episodes", json={"episodes": [episode()]})
    # 同鍵但換了 vault：衝突，且不可因此留下新 vault
    resp = client.post(
        "/v1/episodes", json={"episodes": [episode(vault="folder/other")]}
    ).json()
    assert resp["results"][0]["status"] == "conflict"
    assert resp["created_vaults"] == []
    conn = _db(db_path)
    try:
        keys = [r[0] for r in conn.execute("SELECT key FROM vaults")]
    finally:
        conn.close()
    assert keys == ["github.com/owner/repo-x"]


def test_episode_push_resolves_alias_without_creating(client):
    create_vault(client, "github.com/owner/new-name", aliases=["github.com/owner/old"])
    resp = client.post(
        "/v1/episodes", json={"episodes": [episode(vault="github.com/owner/OLD")]}
    ).json()
    assert resp["created_vaults"] == []
    assert resp["results"][0]["vault"] == "github.com/owner/new-name"


def test_episode_batch_limit(client):
    too_many = [episode(turn_index=i) for i in range(spike_api.EPISODE_BATCH_MAX + 1)]
    resp = client.post("/v1/episodes", json={"episodes": too_many})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_notes_write_still_does_not_auto_create(client):
    client.post("/v1/episodes", json={"episodes": [episode()]})
    resp = client.post(
        "/v1/write", json={"vault": "folder/nope", "title": "t", "body": "b"}
    )
    assert resp.status_code == 404


# ── GET /v1/episodes ────────────────────────────────────────────────


def test_get_episodes_requires_vault(client):
    resp = client.get("/v1/episodes")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "vault_required"


def test_get_episodes_paginates_with_vault_and_filters(client):
    batch = [
        episode(turn_index=i, started_at=f"2026-09-01T0{i}:00:00.000Z", ended_at=None)
        for i in range(5)
    ] + [episode(session_id="s-b", vault="github.com/owner/b", repo=None)]
    client.post("/v1/episodes", json={"episodes": batch})
    seen: list[tuple[str, int]] = []
    cursor = None
    while True:
        params = {"vault": "*", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/episodes", params=params).json()
        seen += [(i["vault"], i["turn_index"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 6
    assert ("github.com/owner/b", 0) in seen
    only_a = client.get(
        "/v1/episodes", params={"vault": "github.com/owner/repo-x"}
    ).json()
    assert {i["vault"] for i in only_a["items"]} == {"github.com/owner/repo-x"}
    since = client.get(
        "/v1/episodes",
        params={"vault": "*", "since": "2026-09-01T03:00:00Z", "session_id": "s-1"},
    ).json()
    assert [i["turn_index"] for i in since["items"]] == [3, 4]
    # repo 為 None 時 display 退回 key
    status = client.post("/v1/vault_resolve", json={"key": "github.com/owner/b"}).json()
    assert status["display"] == "github.com/owner/b"


def test_get_episodes_after_seq_is_insert_ordered_not_started_ordered(client):
    """增量水位用 seq：started_at 很舊但晚到貨的 episode 仍在水位之後（D13）。"""
    client.post(
        "/v1/episodes",
        json={
            "episodes": [
                episode(
                    turn_index=i,
                    started_at=f"2026-09-0{i + 2}T00:00:00Z",
                    ended_at=None,
                )
                for i in range(3)
            ]
        },
    )
    first = client.get("/v1/episodes", params={"vault": "*", "after_seq": 0}).json()
    assert [i["turn_index"] for i in first["items"]] == [0, 1, 2]
    assert first["total"] == 3 and first["next_after_seq"] is None
    watermark = first["max_seq"]
    assert watermark == max(i["seq"] for i in first["items"])
    # 延遲到貨：對話時間比既有全部都早
    client.post(
        "/v1/episodes",
        json={
            "episodes": [
                episode(
                    session_id="s-late",
                    started_at="2026-08-01T00:00:00Z",
                    ended_at=None,
                )
            ]
        },
    )
    late = client.get(
        "/v1/episodes", params={"vault": "*", "after_seq": watermark}
    ).json()
    assert [i["session_id"] for i in late["items"]] == ["s-late"]
    assert late["total"] == 4 and late["max_seq"] > watermark
    # since 模式（對話時間）在水位上會漏掉它——這正是不用 started_at 當水位的原因
    by_time = client.get(
        "/v1/episodes", params={"vault": "*", "since": "2026-09-04T00:00:00Z"}
    ).json()
    assert "s-late" not in {i["session_id"] for i in by_time["items"]}


def test_get_episodes_after_seq_paginates(client):
    client.post(
        "/v1/episodes", json={"episodes": [episode(turn_index=i) for i in range(5)]}
    )
    seen: list[int] = []
    after = 0
    while True:
        page = client.get(
            "/v1/episodes", params={"vault": "*", "after_seq": after, "limit": 2}
        ).json()
        seen += [i["turn_index"] for i in page["items"]]
        if page["next_after_seq"] is None:
            break
        after = page["next_after_seq"]
    assert seen == [0, 1, 2, 3, 4]
    assert page["total"] == 5


def test_get_episodes_after_seq_rejects_mixed_modes(client):
    for extra in (
        {"cursor": "x"},
        {"since": "2026-09-01T00:00:00Z"},
        {"session_id": "s-1"},
    ):
        resp = client.get(
            "/v1/episodes", params={"vault": "*", "after_seq": 0, **extra}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_cursor"
    assert client.get(
        "/v1/episodes", params={"vault": "*", "after_seq": -1}
    ).status_code in (
        400,
        422,
    )


def test_get_episodes_after_seq_does_not_leak_across_vaults(client, monkeypatch):
    client.post(
        "/v1/episodes",
        json={
            "episodes": [
                episode(),
                episode(session_id="s-b", vault="github.com/owner/b"),
            ]
        },
    )

    def leaked() -> tuple[set[str], int]:
        page = client.get(
            "/v1/episodes", params={"vault": "github.com/owner/b", "after_seq": 0}
        ).json()
        return {i["vault"] for i in page["items"]} - {"github.com/owner/b"}, page[
            "total"
        ]

    assert leaked() == (set(), 1)
    monkeypatch.setattr(records, "vault_clause", lambda scope, column: ("1 = 1", ()))
    assert leaked() == ({"github.com/owner/repo-x"}, 2)


def test_get_episodes_bad_cursor(client):
    resp = client.get("/v1/episodes", params={"vault": "*", "cursor": "%%%"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_get_episodes_does_not_leak_across_vaults(client, monkeypatch):
    client.post(
        "/v1/episodes",
        json={
            "episodes": [
                episode(),
                episode(session_id="s-b", vault="github.com/owner/b"),
            ]
        },
    )

    def leaked() -> set[str]:
        page = client.get("/v1/episodes", params={"vault": "github.com/owner/b"}).json()
        return {i["vault"] for i in page["items"]} - {"github.com/owner/b"}

    assert leaked() == set()
    # 拿掉 vault 過濾時要看得到洩漏（證明上面的斷言有在檢查）
    monkeypatch.setattr(records, "vault_clause", lambda scope, column: ("1 = 1", ()))
    assert leaked() == {"github.com/owner/repo-x"}


# ── concepts ────────────────────────────────────────────────────────


def _vaults(client):
    create_vault(client, "github.com/owner/repo-x")
    create_vault(client, "github.com/owner/repo-y")


def test_concepts_round_trip_matches_spike_format(client):
    """匯入 spike 形狀 → export → 逐欄位相等、順序相同。

    相容範圍：頂層 list；每筆鍵集合與值和輸入完全相同。`usability` 只有注入實驗
    寫過才出現（輸入沒有就不輸出）；順序依寫入順序，不依 id 排序。
    """
    _vaults(client)
    source = [
        spike_concept("c-010"),
        spike_concept(
            "c-002",
            scope=None,
            surprisal=0.8,
            probe_result={"verdict": "WRONG", "evidence": "e", "note": "n"},
        ),
        spike_concept("c-1000", scope="Repo-Y", cue=None, why=None),
        spike_concept(
            "c-003",
            surprisal=1.0,
            probe_result={"verdict": "UNKNOWN", "evidence": "證據", "note": None},
            usability={"verdict": "APPLIED", "evidence": "x", "note": None},
        ),
    ]
    items = [dict(c) for c in source]
    items[0]["vault"] = "github.com/owner/repo-x"
    items[2]["vault"] = "github.com/owner/repo-y"
    items[3]["vault"] = "github.com/owner/repo-x"
    resp = client.post("/v1/concepts", json={"vault": "*", "concepts": items})
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 4

    exported = client.get("/v1/concepts/export")
    assert exported.status_code == 200
    data = json.loads(exported.content)
    assert isinstance(data, list)
    assert [c["id"] for c in data] == [c["id"] for c in source]
    for got, want in zip(data, source, strict=True):
        assert list(got) == list(want)  # 鍵集合與順序
        assert got == want
    assert exported.headers[spike_api.HEADER_CONCEPT_COUNT] == "4"
    # 通用 concept 進 global vault（kind=global，自動建立）
    resolved = client.post("/v1/vault_resolve", json={"key": "global"}).json()
    assert resolved["kind"] == "global"


def test_export_default_is_explicit_all_and_vault_filter(client, monkeypatch):
    _vaults(client)
    client.post(
        "/v1/concepts",
        json={
            "vault": "*",
            "concepts": [
                {**spike_concept("c-1"), "vault": "github.com/owner/repo-x"},
                {
                    **spike_concept("c-2", scope="Repo-Y"),
                    "vault": "github.com/owner/repo-y",
                },
            ],
        },
    )
    seen: list[str] = []
    original = records.export_concepts

    def spy(conn, vault):
        seen.append(vault)
        return original(conn, vault)

    monkeypatch.setattr(records, "export_concepts", spy)
    everything = json.loads(client.get("/v1/concepts/export").content)
    assert seen == ["*"]  # 預設是明示的 "*"，不是漏帶 vault
    assert [c["id"] for c in everything] == ["c-1", "c-2"]
    only_y = json.loads(
        client.get(
            "/v1/concepts/export", params={"vault": "github.com/owner/repo-y"}
        ).content
    )
    assert [c["id"] for c in only_y] == ["c-2"]
    assert (
        client.get("/v1/concepts/export", params={"vault": "nope"}).status_code == 404
    )


def test_export_etag_304(client):
    _vaults(client)
    client.post(
        "/v1/concepts",
        json={"vault": "github.com/owner/repo-x", "concepts": [spike_concept("c-1")]},
    )
    first = client.get("/v1/concepts/export")
    etag = first.headers["ETag"]
    again = client.get("/v1/concepts/export", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["ETag"] == etag
    client.post(
        "/v1/concepts",
        json={
            "vault": "*",
            "mode": "update",
            "concepts": [spike_concept("c-1", surprisal=0.4)],
        },
    )
    changed = client.get("/v1/concepts/export", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


def test_export_excludes_missing_scope(client, db_path):
    """scope 缺欄位的 concept（只可能由其他寫入路徑產生）不匯出：
    spike scorer 會把缺鍵當成 None＝通用，放行到所有 repo。"""
    _vaults(client)
    c = connect(db_path, run_migrations=False)
    try:
        records.upsert_concept(
            c, "github.com/owner/repo-x", Concept(id="c-m", statement="s", kind=None)
        )
    finally:
        c.close()
    resp = client.get("/v1/concepts/export")
    assert json.loads(resp.content) == []
    assert resp.headers[spike_api.HEADER_EXCLUDED_MISSING_SCOPE] == "1"


def test_concepts_order_survives_update_and_delete(client):
    _vaults(client)
    v = "github.com/owner/repo-x"
    client.post(
        "/v1/concepts",
        json={
            "vault": v,
            "concepts": [spike_concept(i) for i in ("c-3", "c-1", "c-2")],
        },
    )
    resp = client.post(
        "/v1/concepts",
        json={
            "vault": v,
            "concepts": [spike_concept("c-1", surprisal=0.8), spike_concept("c-0")],
            "delete": ["c-3", "c-gone"],
        },
    ).json()
    assert (resp["updated"], resp["created"], resp["deleted"], resp["not_found"]) == (
        1,
        1,
        1,
        1,
    )
    data = json.loads(client.get("/v1/concepts/export").content)
    assert [c["id"] for c in data] == ["c-1", "c-2", "c-0"]
    assert data[0]["surprisal"] == 0.8
    unchanged = client.post(
        "/v1/concepts", json={"vault": v, "concepts": [spike_concept("c-2")]}
    ).json()
    assert unchanged["unchanged"] == 1


def test_concepts_batch_is_all_or_nothing(client):
    _vaults(client)
    v = "github.com/owner/repo-x"
    client.post("/v1/concepts", json={"vault": v, "concepts": [spike_concept("c-1")]})
    resp = client.post(
        "/v1/concepts",
        json={
            "vault": "*",
            "concepts": [
                {**spike_concept("c-new"), "vault": v},
                # 既有 id 帶了別的 vault：歸屬凍結 → conflict
                {**spike_concept("c-1"), "vault": "github.com/owner/repo-y"},
            ],
            "delete": ["c-1"],
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "batch_rejected"
    assert [r["status"] for r in err["results"]] == ["created", "conflict"]
    data = json.loads(client.get("/v1/concepts/export").content)
    assert [c["id"] for c in data] == ["c-1"]  # 什麼都沒寫、也沒刪


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ({k: v for k, v in spike_concept("c-x").items() if k != "scope"}, "scope"),
        ({**spike_concept("c-x"), "extra": 1}, "未知欄位"),
        (spike_concept("c-x", scope="global"), "null"),
        # 新 concept 未帶 vault、批次為 "*"，且 A17 的 source_turns／scope 都對不上
        (spike_concept("c-x", scope="Nowhere"), "vault"),
        (
            {**spike_concept("c-x", scope=None), "vault": "github.com/owner/repo-x"},
            "global",
        ),
    ],
)
def test_concepts_invalid_items(client, item, reason):
    _vaults(client)
    resp = client.post("/v1/concepts", json={"vault": "*", "concepts": [item]})
    assert resp.status_code == 400, resp.text
    result = resp.json()["error"]["results"][0]
    assert result["status"] == "invalid"
    assert reason in result["error"]


def test_concepts_modes_guard_id_collisions(client):
    _vaults(client)
    v = "github.com/owner/repo-x"
    client.post("/v1/concepts", json={"vault": v, "concepts": [spike_concept("c-1")]})
    collide = client.post(
        "/v1/concepts",
        json={
            "vault": v,
            "mode": "create",
            "concepts": [spike_concept("c-1", statement="另一條")],
        },
    )
    assert collide.status_code == 409
    missing = client.post(
        "/v1/concepts",
        json={"vault": v, "mode": "update", "concepts": [spike_concept("c-9")]},
    )
    assert missing.status_code == 400
    dup = client.post(
        "/v1/concepts",
        json={"vault": v, "concepts": [spike_concept("c-5"), spike_concept("c-5")]},
    )
    assert dup.status_code == 400


def test_concepts_require_explicit_vault(client):
    resp = client.post("/v1/concepts", json={"concepts": [spike_concept("c-1")]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "vault_required"


def test_single_vault_batch_cannot_touch_other_vault(client):
    _vaults(client)
    x, y = "github.com/owner/repo-x", "github.com/owner/repo-y"
    client.post("/v1/concepts", json={"vault": y, "concepts": [spike_concept("c-y")]})
    resp = client.post("/v1/concepts", json={"vault": x, "delete": ["c-y"]}).json()
    assert resp["delete_results"] == [{"id": "c-y", "status": "not_found"}]
    upd = client.post(
        "/v1/concepts", json={"vault": x, "concepts": [spike_concept("c-y")]}
    )
    assert upd.status_code == 409
    data = json.loads(client.get("/v1/concepts/export").content)
    assert [c["id"] for c in data] == ["c-y"]


def test_existing_concept_keeps_vault_when_rescoped_global(client):
    """spike backfill 會把 scope 改成 None；歸屬凍結，不搬進 global。"""
    _vaults(client)
    x = "github.com/owner/repo-x"
    client.post("/v1/concepts", json={"vault": x, "concepts": [spike_concept("c-1")]})
    resp = client.post(
        "/v1/concepts",
        json={"vault": "*", "concepts": [spike_concept("c-1", scope=None)]},
    ).json()
    assert resp["results"][0] == {
        "index": 0,
        "id": "c-1",
        "vault": x,
        "resolved_by": "existing",
        "status": "updated",
    }
    assert json.loads(client.get("/v1/concepts/export").content)[0]["scope"] is None


# ── POST /v1/injections ─────────────────────────────────────────────


def test_injections_idempotent_and_unknown_vault(client, db_path):
    create_vault(client, "github.com/owner/repo-x")
    v = "github.com/owner/repo-x"
    items = [
        {"session_id": "s", "prompt_id": "p", "injected": ["c-1"], "vault": v},
        {
            "session_id": "s",
            "prompt_id": None,
            "prompt_fingerprint": "fp",
            "injected": [],
            "vault": v,
            "recorded": "2026-09-01T00:00:00Z",
        },
        {
            "session_id": "s",
            "prompt_id": "p",
            "injected": ["c-1"],
            "vault": "folder/new",
        },
        {"session_id": "s", "injected": ["c-1"], "vault": v},  # 缺 prompt_id 鍵
    ]
    first = client.post("/v1/injections", json={"injections": items}).json()
    assert [r["status"] for r in first["results"]] == [
        "accepted",
        "accepted",
        "unknown_vault",
        "invalid",
    ]
    again = client.post("/v1/injections", json={"injections": copy.deepcopy(items[:2])})
    assert again.json()["duplicates"] == 2
    conn = _db(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM injections").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM vaults").fetchone()[0] == 1
    finally:
        conn.close()


def test_new_endpoints_require_auth(client):
    anon = client.__class__(client.app)
    for method, path in (
        ("post", "/v1/episodes"),
        ("get", "/v1/episodes"),
        ("get", "/v1/concepts/export"),
        ("post", "/v1/concepts"),
        ("post", "/v1/injections"),
    ):
        resp = getattr(anon, method)(path)
        assert resp.status_code == 401, path
