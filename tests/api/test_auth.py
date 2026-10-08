"""T-28：bearer token 認證（A15）與「未設定 token 拒絕啟動」。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.api.auth import BearerAuthMiddleware
from lore_vault.config import ConfigError, Secret

from .conftest import TOKEN, make_settings

ENDPOINTS = [
    ("post", "/v1/vault_resolve", {"key": "folder/a"}),
    ("post", "/v1/vaults", {"key": "folder/a", "display": "a"}),
    ("post", "/v1/recall", {"query": "q", "vault": "folder/a"}),
    ("post", "/v1/ask", {"question": "q", "vault": "folder/a"}),
    ("post", "/v1/get", {"vault": "folder/a", "ids": ["x"]}),
    ("post", "/v1/list", {"vault": "folder/a"}),
    ("post", "/v1/write", {"vault": "folder/a", "title": "t", "body": "b"}),
    (
        "post",
        "/v1/update",
        {"vault": "folder/a", "id": "x", "expected_updated": "t", "title": "x"},
    ),
    ("post", "/v1/status", None),
    ("post", "/v1/documents", None),
    ("get", "/v1/openapi.json", None),
    # 不存在的路徑也先過認證：未認證者連 404 都拿不到
    ("get", "/v1/nope", None),
    ("get", "/docs", None),
]

BAD_HEADERS = [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer wrong-token-0123456789"},
    {"Authorization": f"Bearer {TOKEN}x"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN},
]


@pytest.fixture
def anon(db_path):
    with TestClient(create_app(make_settings(db_path))) as c:
        yield c


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
@pytest.mark.parametrize("headers", BAD_HEADERS)
def test_unauthenticated_requests_are_rejected(anon, method, path, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(anon, method)(path, **kwargs)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "unauthorized"
    assert TOKEN not in resp.text


def test_malformed_body_without_token_is_401_not_422(anon):
    resp = anon.post(
        "/v1/recall", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401


def test_valid_token_is_accepted_including_scheme_case(anon):
    for scheme in ("Bearer", "bearer", "BEARER"):
        resp = anon.post("/v1/status", headers={"Authorization": f"{scheme} {TOKEN}"})
        assert resp.status_code == 200, resp.text


def test_localhost_requests_also_need_token(db_path):
    """本機請求不免認證（A15）：TestClient 的 client 位址不影響判斷。"""
    app = create_app(make_settings(db_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.post("/v1/status").status_code == 401


def test_duplicate_authorization_headers_are_rejected(anon):
    resp = anon.post(
        "/v1/status",
        headers=[
            ("Authorization", f"Bearer {TOKEN}"),
            ("Authorization", f"Bearer {TOKEN}"),
        ],
    )
    assert resp.status_code == 401


def test_healthz_needs_no_token(anon):
    assert anon.get("/healthz").status_code == 200


def test_openapi_is_served_behind_auth(anon):
    resp = anon.get("/v1/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    paths = set(resp.json()["paths"])
    assert {
        "/v1/vault_resolve",
        "/v1/recall",
        "/v1/ask",
        "/v1/get",
        "/v1/list",
        "/v1/write",
        "/v1/update",
        "/v1/status",
        "/v1/vaults",
        "/v1/documents",
        "/v1/document_download",
        "/v1/snapshot",
        "/v1/episodes",
        "/v1/concepts",
        "/v1/concepts/export",
        "/v1/injections",
        # UI 管理端點（api.manage）
        "/v1/vault_list",
        "/v1/vault_update",
        "/v1/vault_alias_add",
        "/v1/vault_alias_remove",
        "/v1/vault_move_space",
        "/v1/vault_delete",
        "/v1/note_delete",
        "/v1/document_delete",
        "/v1/tombstones",
        "/v1/note_undelete",
        "/v1/document_undelete",
        "/v1/document_retry",
        "/v1/concept_query",
        "/v1/episode_summary",
        "/v1/topics",
        # 執行期設定（D13；只允許 UI session）
        "/v1/settings",
        "/v1/settings_update",
        "/v1/settings_reset",
        # 側載小型機器狀態（schema v17）
        "/v1/blob_put",
        "/v1/blob_get",
        # 任務層授權紀錄（MCP-T5；只允許 UI session）
        "/v1/tasks_authorize",
        "/v1/tasks_authorization_status",
    } == paths


def test_auth_uses_constant_time_compare(monkeypatch, anon):
    # 比對在憑證 → principal 對照表（A22）裡做
    import lore_vault.api.principals as auth

    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    anon.post("/v1/status", headers={"Authorization": "Bearer nope-nope-nope-nope"})
    assert calls == [(b"nope-nope-nope-nope", TOKEN.encode())]


# ── 啟動 ────────────────────────────────────────────────────────────


def _environ(tmp_path, **extra):
    env = {"LORE_VAULT_DATABASE_PATH": str(tmp_path / "lore.db")}
    env.update(extra)
    return env


def test_missing_token_uses_generated_secret_file(tmp_path):
    """D12：未設（或空白）不再拒絕啟動，改用資料目錄 secrets/api-token（首次產生）；
    詳細行為見 test_bootstrap.py。建 app 不碰資料庫（遷移在 lifespan）。"""
    create_app(environ=_environ(tmp_path))
    token_file = tmp_path / "secrets" / "api-token"
    generated = token_file.read_text(encoding="utf-8").strip()
    assert len(generated) >= 16
    create_app(environ=_environ(tmp_path, LORE_VAULT_API_TOKEN="   "))
    assert token_file.read_text(encoding="utf-8").strip() == generated
    assert not (tmp_path / "lore.db").exists()


def test_empty_token_file_refuses_to_start(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api-token").write_text("  ", encoding="utf-8")
    with pytest.raises(ConfigError, match="LORE_VAULT_API_TOKEN"):
        create_app(environ=_environ(tmp_path))


@pytest.mark.parametrize("bad", ["short", "has space 0123456789abc"])
def test_weak_token_refuses_to_start(tmp_path, bad):
    with pytest.raises(ConfigError) as info:
        create_app(environ=_environ(tmp_path, LORE_VAULT_API_TOKEN=bad))
    assert bad not in str(info.value)


def test_directly_built_settings_are_validated_too(db_path):
    with pytest.raises(ConfigError):
        create_app(make_settings(db_path, token=Secret("")))


def test_missing_db_path_refuses_to_start():
    with pytest.raises(ConfigError, match="資料庫路徑"):
        create_app(environ={"LORE_VAULT_API_TOKEN": TOKEN})


def test_factory_from_environ_serves_requests(tmp_path):
    app = create_app(
        environ=_environ(
            tmp_path, LORE_VAULT_API_TOKEN=TOKEN, LORE_VAULT_API_ENRICH_WORKER="false"
        )
    )
    with TestClient(app) as c:
        resp = c.post("/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        assert resp.json()["enrich"]["worker"]["enabled"] is False
    assert (tmp_path / "lore.db").exists()  # lifespan 啟動時已遷移


def test_token_never_appears_in_logs_or_repr(db_path, caplog):
    caplog.set_level(logging.DEBUG)
    app = create_app(make_settings(db_path))
    with TestClient(app) as c:
        c.post("/v1/status", headers={"Authorization": "Bearer wrong-wrong-wrong-1"})
        c.post("/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
        c.post(
            "/v1/recall",
            json={"query": "", "vault": "x"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(make_settings(db_path))
    middleware = BearerAuthMiddleware(app, Secret(TOKEN))
    assert TOKEN not in repr(middleware)
