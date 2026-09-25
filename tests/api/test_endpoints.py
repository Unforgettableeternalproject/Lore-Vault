"""T-24／T-25：每個端點的成功與錯誤路徑、錯誤映射、degraded 旗標穿透。"""

from __future__ import annotations

import pytest

from .conftest import (
    RaisingEmbedder,
    create_vault,
    embed_all,
    write_note,
)

A = "github.com/owner/alpha"


@pytest.fixture
def seeded(client):
    create_vault(client, A, aliases=["github.com/owner/old-alpha"])
    note = write_note(
        client, A, "中文檢索設計", "FTS5 用 CJK bigram 切詞，記憶系統核心。"
    )
    return note


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    return body["error"]


# ── vault_resolve／建 vault ──────────────────────────────────────────


def test_vault_resolve_returns_key_display_and_note_count(client, seeded):
    resp = client.post("/v1/vault_resolve", json={"key": A})
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == A
    assert data["display"] == A
    assert data["note_count"] == 1
    assert data["via_alias"] is False


def test_vault_resolve_follows_alias_and_case(client, seeded):
    data = client.post(
        "/v1/vault_resolve", json={"key": "GitHub.com/Owner/Old-Alpha"}
    ).json()
    assert data["key"] == A
    assert data["via_alias"] is True


def test_vault_resolve_unknown_is_404_with_create_hint(client):
    err = _error(
        client.post("/v1/vault_resolve", json={"key": "folder/nope"}),
        404,
        "unknown_vault",
    )
    assert "POST /v1/vaults" in err["hint"]


@pytest.mark.parametrize("key", ["", "*"])
def test_vault_resolve_rejects_empty_and_wildcard(client, key):
    _error(client.post("/v1/vault_resolve", json={"key": key}), 400, "vault_required")


def test_create_vault_is_create_only(client, seeded):
    err = _error(
        client.post("/v1/vaults", json={"key": A, "display": "改名"}),
        409,
        "vault_exists",
    )
    assert err["existing"]["key"] == A
    # 既有 display 與別名沒被覆寫
    data = client.post("/v1/vault_resolve", json={"key": A}).json()
    assert data["display"] == A
    assert data["aliases"] == ["github.com/owner/old-alpha"]


def test_create_vault_rejects_alias_owned_by_other_vault(client, seeded):
    _error(
        client.post(
            "/v1/vaults",
            json={
                "key": "folder/b",
                "display": "b",
                "aliases": ["github.com/owner/old-alpha"],
            },
        ),
        409,
        "vault_exists",
    )


def test_create_vault_rejects_wildcard_and_bad_kind(client):
    _error(
        client.post("/v1/vaults", json={"key": "*", "display": "all"}),
        400,
        "vault_required",
    )
    _error(
        client.post(
            "/v1/vaults", json={"key": "folder/x", "display": "x", "kind": "weird"}
        ),
        400,
        "invalid_request",
    )


def test_write_does_not_create_vault(client):
    _error(
        client.post(
            "/v1/write", json={"vault": "folder/ghost", "title": "t", "body": "b"}
        ),
        404,
        "unknown_vault",
    )
    _error(
        client.post("/v1/vault_resolve", json={"key": "folder/ghost"}),
        404,
        "unknown_vault",
    )


# ── recall ──────────────────────────────────────────────────────────


def test_recall_returns_index_without_body(client, db_path, seeded):
    embed_all(db_path)
    data = client.post("/v1/recall", json={"query": "記憶", "vault": A}).json()
    assert [i["id"] for i in data["items"]] == [seeded["id"]]
    item = data["items"][0]
    assert "body" not in item
    assert set(item) == {
        "id",
        "kind",
        "vault",
        "title",
        "summary",
        "summary_source",
        "score",
        "updated",
    }
    assert data["degraded"] is False
    assert set(data["legs"]) == {"lexical", "vector"}


def test_recall_budget_truncation_is_flagged(client, seeded):
    write_note(client, A, "第二篇記憶", "記憶的另一篇內容" * 5)
    data = client.post(
        "/v1/recall", json={"query": "記憶", "vault": A, "budget": 10}
    ).json()
    assert data["truncated"] is True
    assert data["used_chars"] <= 10


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (TimeoutError("slow"), "embedder_timeout"),
        (ConnectionError("down"), "embedder_error"),
    ],
)
def test_recall_degraded_flag_passes_through(make_client, exc, reason):
    c = make_client(query_embedder=RaisingEmbedder(exc))
    create_vault(c, A)
    write_note(c, A, "記憶設計", "記憶系統")
    data = c.post("/v1/recall", json={"query": "記憶", "vault": A}).json()
    assert data["degraded"] is True
    assert data["degraded_reason"] == reason
    assert data["legs"] == ["lexical"]
    assert len(data["items"]) == 1


def test_recall_errors(client, seeded):
    _error(client.post("/v1/recall", json={"query": "記憶"}), 400, "vault_required")
    _error(
        client.post("/v1/recall", json={"query": "記憶", "vault": "folder/x"}),
        404,
        "unknown_vault",
    )
    _error(
        client.post("/v1/recall", json={"query": "  ", "vault": A}),
        400,
        "invalid_request",
    )
    _error(
        client.post("/v1/recall", json={"query": "記憶", "vault": A, "limit": 0}),
        400,
        "invalid_request",
    )
    err = _error(
        client.post(
            "/v1/recall", json={"query": "記憶", "vault": A, "kinds": ["concept"]}
        ),
        400,
        "unsupported_kind",
    )
    assert "concept" in err["message"]


def test_recall_partially_unsupported_kinds_are_reported(client, seeded):
    data = client.post(
        "/v1/recall",
        json={"query": "記憶", "vault": A, "kinds": ["note", "concept"]},
    ).json()
    assert data["unsupported_kinds"] == ["concept"]


def test_unknown_request_field_is_rejected(client, seeded):
    resp = client.post("/v1/recall", json={"query": "記憶", "vault": A, "vaul": A})
    assert resp.status_code == 422


# ── get ─────────────────────────────────────────────────────────────


def test_get_returns_full_body_and_missing(client, seeded):
    data = client.post(
        "/v1/get", json={"vault": A, "ids": [seeded["id"], "nope"]}
    ).json()
    assert data["items"][0]["body"].startswith("FTS5")
    assert data["missing"] == ["nope"]
    assert data["truncated"] is False


def test_get_budget_truncates(client, seeded):
    data = client.post(
        "/v1/get", json={"vault": A, "ids": [seeded["id"]], "budget": 5}
    ).json()
    assert data["truncated"] is True
    assert len(data["items"][0]["body"]) == 5


def test_get_errors(client, seeded):
    _error(client.post("/v1/get", json={"ids": ["x"]}), 400, "vault_required")
    _error(client.post("/v1/get", json={"vault": A, "ids": []}), 400, "invalid_request")
    _error(
        client.post("/v1/get", json={"vault": "folder/x", "ids": ["x"]}),
        404,
        "unknown_vault",
    )


# ── list ────────────────────────────────────────────────────────────


def test_list_paginates(client, seeded):
    write_note(client, A, "第二篇", "內容二")
    first = client.post("/v1/list", json={"vault": A, "limit": 1}).json()
    assert len(first["items"]) == 1
    assert first["has_more"] is True
    second = client.post(
        "/v1/list", json={"vault": A, "limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert second["has_more"] is False
    assert {first["items"][0]["id"], second["items"][0]["id"]} >= {seeded["id"]}
    assert "body" not in first["items"][0]


def test_list_errors(client, seeded):
    _error(client.post("/v1/list", json={}), 400, "vault_required")
    _error(
        client.post("/v1/list", json={"vault": A, "cursor": "!!!"}),
        400,
        "invalid_cursor",
    )
    _error(
        client.post("/v1/list", json={"vault": A, "limit": 9999}),
        400,
        "invalid_request",
    )


# ── write ───────────────────────────────────────────────────────────


def test_write_reports_duplicates(client, seeded):
    data = write_note(
        client, A, "中文檢索設計", "FTS5 用 CJK bigram 切詞，記憶系統核心。"
    )
    assert data["vault"] == A
    assert [d["id"] for d in data["duplicates"]] == [seeded["id"]]
    assert data["dedup_degraded"] is False


def test_write_dedup_degraded_flag_passes_through(make_client):
    c = make_client(query_embedder=RaisingEmbedder(TimeoutError("slow")))
    create_vault(c, A)
    data = write_note(c, A, "t", "b")
    assert data["dedup_degraded"] is True
    assert data["dedup_reason"] == "embedder_timeout"


def test_write_errors(client, seeded):
    _error(
        client.post("/v1/write", json={"title": "t", "body": "b"}),
        400,
        "vault_required",
    )
    _error(
        client.post("/v1/write", json={"vault": "*", "title": "t", "body": "b"}),
        400,
        "vault_required",
    )
    _error(
        client.post(
            "/v1/write",
            json={"vault": A, "title": "t", "body": "b", "supersedes": "nope"},
        ),
        404,
        "not_found",
    )
    _error(
        client.post("/v1/write", json={"vault": A, "title": "  ", "body": "b"}),
        400,
        "invalid_request",
    )


# ── update ──────────────────────────────────────────────────────────


def test_update_success_and_version_conflict(client, seeded):
    resp = client.post(
        "/v1/update",
        json={
            "vault": A,
            "id": seeded["id"],
            "expected_updated": seeded["updated"],
            "body": "新內容",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary_stale"] is True
    assert data["embedding_stale"] is True
    assert data["updated"] > seeded["updated"]

    err = _error(
        client.post(
            "/v1/update",
            json={
                "vault": A,
                "id": seeded["id"],
                "expected_updated": seeded["updated"],
                "body": "拿舊版本覆蓋",
            },
        ),
        409,
        "version_conflict",
    )
    assert err["current"]["updated"] == data["updated"]
    assert err["expected"] == seeded["updated"]
    assert "body" not in err["current"]
    body = client.post("/v1/get", json={"vault": A, "ids": [seeded["id"]]}).json()
    assert body["items"][0]["body"] == "新內容"


def test_update_supersedes_can_be_cleared_explicitly(client, seeded):
    newer = write_note(client, A, "新版", "內容", supersedes=seeded["id"])
    resp = client.post(
        "/v1/update",
        json={
            "vault": A,
            "id": newer["id"],
            "expected_updated": newer["updated"],
            "supersedes": None,
        },
    )
    assert resp.status_code == 200, resp.text
    got = client.post("/v1/get", json={"vault": A, "ids": [newer["id"]]}).json()
    assert got["items"][0]["supersedes"] is None


def test_update_errors(client, seeded):
    base = {"vault": A, "id": seeded["id"], "expected_updated": seeded["updated"]}
    _error(client.post("/v1/update", json=base), 400, "no_changes")
    _error(
        client.post("/v1/update", json={**base, "id": "nope", "title": "x"}),
        404,
        "not_found",
    )
    _error(
        client.post("/v1/update", json={**base, "vault": None, "title": "x"}),
        400,
        "vault_required",
    )


# ── status ──────────────────────────────────────────────────────────


def test_status_merges_doctor_backlog_and_schema(client, seeded):
    resp = client.post("/v1/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["schema"]["version"] == data["schema"]["expected"]
    names = {c["name"] for c in data["doctor"]["checks"]}
    from lore_vault.doctor import default_registry

    assert names == {c.name for c in default_registry().checks}
    assert data["enrich"]["backlog"]["counts"]["summary_pending"] == 1
    assert data["enrich"]["worker"] == {"enabled": False, "running": False}
    assert data["vault"] is None
    assert data["ok"] == data["doctor"]["ok"]


def test_status_with_vault(client, seeded):
    data = client.post("/v1/status", json={"vault": A}).json()
    assert data["vault"]["key"] == A
    assert data["vault"]["note_count"] == 1
    assert data["vault"]["last_updated"] == seeded["updated"]
    _error(client.post("/v1/status", json={"vault": "folder/x"}), 404, "unknown_vault")


def test_status_reflects_failing_doctor_check(client, db_path, seeded):
    """doctor 紅燈要穿透到 status：拔掉 FTS 列 → storage.fts_rows fail、ok=false。"""
    from lore_vault.storage.db import connect

    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM note_fts")
    finally:
        conn.close()
    data = client.post("/v1/status").json()
    assert data["ok"] is False
    by_name = {c["name"]: c for c in data["doctor"]["checks"]}
    assert by_name["storage.fts_rows"]["status"] == "fail"


def test_healthz_is_public_and_minimal(client):
    client.headers.pop("Authorization")
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_status_passes_backup_settings_to_doctor(make_client, tmp_path):
    """設定了備份目錄但從未備份：backup.recent 為 fail，status 的 ok 跟著變 false。"""
    from lore_vault.config import BackupConfig, Config, EmbeddingConfig

    from .conftest import DIM

    backups = tmp_path / "backups"
    backups.mkdir()
    c = make_client(
        config=Config(
            embedding=EmbeddingConfig(dim=DIM), backup=BackupConfig(dir=str(backups))
        )
    )
    data = c.post("/v1/status").json()
    by_name = {x["name"]: x for x in data["doctor"]["checks"]}
    assert by_name["backup.recent"]["status"] == "fail"
    assert data["ok"] is False


def test_unhandled_storage_error_is_500_not_400(client, seeded, monkeypatch):
    """DimensionMismatch 等同時是 ValueError，但屬伺服器資料問題，不能回 400。"""
    from lore_vault.api import routes
    from lore_vault.storage.errors import DimensionMismatch

    def broken(*args, **kwargs):
        raise DimensionMismatch("庫內向量維度不符")

    monkeypatch.setattr(routes, "recall_service", broken)
    _error(
        client.post("/v1/recall", json={"query": "記憶", "vault": A}),
        500,
        "storage_error",
    )
