"""A21：UI 本地身分驗證（session cookie、CSRF、限流）、/ui 靜態檔與安全標頭。"""

from __future__ import annotations

import logging
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import Config, ConfigError, EmbeddingConfig, UiConfig

from .conftest import AUTH, DIM, TOKEN, make_settings

UI = {"X-Lore-Vault-UI": "1"}
SECURE_COOKIE = "__Host-lv_session"
PLAIN_COOKIE = "lv_session"
START = 1_800_000_000.0


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def static_dir(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><div id="app"></div>', encoding="utf-8"
    )
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return root


@pytest.fixture
def make_ui(db_path, clock, static_dir):
    """UI 用 client：https base_url（httpx 才回送 Secure cookie），不預帶 bearer。"""
    opened: list[TestClient] = []

    def make(*, client=("10.0.0.1", 50000), base_url="https://testserver", **ui):
        ui.setdefault("static_dir", str(static_dir))
        config = Config(embedding=EmbeddingConfig(dim=DIM), ui=UiConfig(**ui))
        app = create_app(make_settings(db_path, config=config, clock=clock))
        c = TestClient(app, base_url=base_url, client=client)
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def ui(make_ui):
    return make_ui()


def login(client: TestClient, key: str = TOKEN, headers=UI):
    return client.post("/ui/api/login", json={"key": key}, headers=headers)


def parse_set_cookie(resp) -> tuple[str, str, dict[str, str]]:
    """回傳 (名稱, 值, 屬性)；屬性鍵一律小寫，旗標屬性值為 "true"。"""
    raw = resp.headers["set-cookie"]
    parts = [p.strip() for p in raw.split(";")]
    name, _, value = parts[0].partition("=")
    attrs: dict[str, str] = {}
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        attrs[key.lower()] = val if sep else "true"
    return name, value, attrs


# ── 登入 ────────────────────────────────────────────────────────────


def test_login_sets_hardened_session_cookie(ui):
    resp = login(ui)
    assert resp.status_code == 204
    name, value, attrs = parse_set_cookie(resp)
    assert name == SECURE_COOKIE
    assert len(value) >= 40
    assert TOKEN not in resp.headers["set-cookie"]
    assert attrs["httponly"] == "true"
    assert attrs["secure"] == "true"
    assert attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/"
    assert "domain" not in attrs
    assert attrs["max-age"] == str(12 * 3600)


def test_cookie_secure_can_be_disabled_for_local_http(make_ui):
    c = make_ui(cookie_secure=False, base_url="http://testserver")
    resp = login(c)
    assert resp.status_code == 204
    name, _, attrs = parse_set_cookie(resp)
    assert name == PLAIN_COOKIE
    assert "secure" not in attrs
    assert attrs["httponly"] == "true"
    assert attrs["samesite"].lower() == "strict"
    assert c.post("/v1/status", headers=UI).status_code == 200


def test_wrong_key_is_rejected_without_session(ui):
    resp = login(ui, key="wrong-key-0123456789abcdef")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in resp.headers
    assert ui.post("/v1/status", headers=UI).status_code == 401


def test_login_requires_csrf_header(ui):
    resp = login(ui, headers={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_required"
    assert "set-cookie" not in resp.headers


@pytest.mark.parametrize(
    "content",
    [b"", b"{not json", b'"just a string"', b'{"key": 1}', b'{"key": "x", "y": 1}'],
)
def test_malformed_login_body_is_400(ui, content):
    resp = ui.post(
        "/ui/api/login",
        content=content,
        headers={**UI, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_oversized_login_body_is_413(ui):
    resp = ui.post(
        "/ui/api/login",
        content=b'{"key": "' + b"a" * 10_000 + b'"}',
        headers={**UI, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "too_large"


def test_login_attempts_are_logged_without_key(ui, caplog):
    caplog.set_level(logging.INFO, logger="lore_vault")
    login(ui, key="guess-guess-guess-guess-1")
    login(ui)
    messages = [r.getMessage() for r in caplog.records if r.name.endswith("api.ui")]
    assert any("登入失敗" in m and "10.0.0.1" in m for m in messages)
    assert any("登入成功" in m for m in messages)
    assert TOKEN not in caplog.text
    assert "guess-guess-guess-guess-1" not in caplog.text


# ── cookie 認證與 CSRF ───────────────────────────────────────────────


def test_session_cookie_authenticates_v1_with_ui_header(ui):
    assert ui.post("/v1/status", headers=UI).status_code == 401
    login(ui)
    resp = ui.post("/v1/status", headers=UI)
    assert resp.status_code == 200, resp.text
    session = ui.get("/ui/api/session", headers=UI)
    assert session.status_code == 200
    assert session.json()["authenticated"] is True


def test_cookie_without_csrf_header_is_blocked(ui):
    """拿掉 CSRF 標頭檢查時這個測試會紅：cookie 有效也不能無標頭存取 /v1。"""
    login(ui)
    resp = ui.post("/v1/status")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_required"
    assert ui.post("/v1/status", headers={"X-Lore-Vault-UI": "0"}).status_code == 403
    dup = [("X-Lore-Vault-UI", "1"), ("X-Lore-Vault-UI", "1")]
    assert ui.post("/v1/status", headers=dup).status_code == 403


def test_forged_or_unknown_cookie_is_401(ui):
    ui.cookies.set(SECURE_COOKIE, "forged-session-id", domain="testserver.local")
    assert ui.post("/v1/status", headers=UI).status_code == 401
    assert ui.get("/ui/api/session", headers=UI).status_code == 401


def test_session_endpoints_require_csrf_header(ui):
    login(ui)
    assert ui.get("/ui/api/session").status_code == 403
    assert ui.post("/ui/api/logout").status_code == 403
    # 無標頭的登出不生效
    assert ui.post("/v1/status", headers=UI).status_code == 200


def test_idle_timeout_expires_session(ui, clock):
    login(ui)
    clock.advance(59 * 60)
    assert ui.post("/v1/status", headers=UI).status_code == 200
    clock.advance(59 * 60)  # 距上次使用 59 分鐘：仍有效（使用會延長閒置期限）
    assert ui.post("/v1/status", headers=UI).status_code == 200
    clock.advance(60 * 60)
    assert ui.post("/v1/status", headers=UI).status_code == 401
    assert ui.get("/ui/api/session", headers=UI).status_code == 401


def test_absolute_timeout_expires_even_when_active(ui, clock):
    login(ui)
    for _ in range(23):  # 每 30 分鐘用一次，共 11.5 小時
        clock.advance(30 * 60)
        assert ui.post("/v1/status", headers=UI).status_code == 200
    clock.advance(30 * 60)  # 滿 12 小時
    assert ui.post("/v1/status", headers=UI).status_code == 401


def test_session_endpoint_reports_expiry(ui, clock):
    login(ui)
    clock.advance(10)
    body = ui.get("/ui/api/session", headers=UI).json()
    assert body["expires_at"].endswith("Z")
    assert body["idle_expires_at"] < body["expires_at"]


def test_logout_revokes_session_and_clears_cookie(ui):
    login(ui)
    old = ui.cookies.get(SECURE_COOKIE)
    # 對照組：手動放回的 cookie 確實會被送出且有效
    ui.cookies.clear()
    ui.cookies.set(SECURE_COOKIE, old, domain="testserver.local")
    assert ui.post("/v1/status", headers=UI).status_code == 200
    resp = ui.post("/ui/api/logout", headers=UI)
    assert resp.status_code == 204
    name, value, attrs = parse_set_cookie(resp)
    assert name == SECURE_COOKIE
    assert attrs["max-age"] == "0"
    assert attrs["path"] == "/"
    assert attrs["secure"] == "true"
    # 就算客戶端留著舊 cookie，服務端已註銷
    ui.cookies.set(SECURE_COOKIE, old, domain="testserver.local")
    assert ui.post("/v1/status", headers=UI).status_code == 401
    # 沒有 session 也能登出（冪等）
    assert ui.post("/ui/api/logout", headers=UI).status_code == 204


def test_sessions_do_not_survive_restart(make_ui):
    first = make_ui()
    login(first)
    cookie = first.cookies.get(SECURE_COOKIE)
    second = make_ui()  # 新 app = 服務重啟
    second.cookies.set(SECURE_COOKIE, cookie, domain="testserver.local")
    assert second.post("/v1/status", headers=UI).status_code == 401


def test_session_cap_evicts_oldest(make_ui, clock):
    c = make_ui(max_sessions=2)
    cookies = []
    for _ in range(3):
        login(c)
        cookies.append(c.cookies.get(SECURE_COOKIE))
        c.cookies.clear()
        clock.advance(1)
    c.cookies.set(SECURE_COOKIE, cookies[0], domain="testserver.local")
    assert c.post("/v1/status", headers=UI).status_code == 401
    c.cookies.set(SECURE_COOKIE, cookies[2], domain="testserver.local")
    assert c.post("/v1/status", headers=UI).status_code == 200


# ── Bearer 路徑不變 ─────────────────────────────────────────────────


def test_bearer_path_needs_no_ui_header_or_cookie(ui):
    assert ui.post("/v1/status", headers=AUTH).status_code == 200


def test_authorization_header_disables_cookie_fallback(ui):
    """帶了 Authorization 就只看 bearer：錯的 bearer 不會被有效 cookie 救回。"""
    login(ui)
    bad = {**UI, "Authorization": "Bearer wrong-token-0123456789"}
    resp = ui.post("/v1/status", headers=bad)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_ui_login_does_not_open_non_v1_paths_without_auth(ui):
    assert ui.get("/docs").status_code == 401
    assert ui.get("/v1/nope").status_code == 401


# ── 限流 ────────────────────────────────────────────────────────────


def _fail(c: TestClient, n: int, headers=UI):
    for _ in range(n):
        resp = c.post(
            "/ui/api/login", json={"key": "bad-bad-bad-bad-bad"}, headers=headers
        )
        assert resp.status_code == 401, resp.text


def test_per_ip_limit_then_exponential_backoff(ui, clock):
    _fail(ui, 5)
    resp = login(ui)  # 正確金鑰也擋：退避期間不檢查金鑰
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "too_many_attempts"
    assert resp.headers["retry-after"] == "60"
    assert "set-cookie" not in resp.headers
    clock.advance(61)
    _fail(ui, 1)  # 第 6 次失敗：退避加倍
    resp = login(ui)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "120"
    clock.advance(121)
    assert login(ui).status_code == 204


def test_success_resets_ip_counter(ui):
    _fail(ui, 4)
    assert login(ui).status_code == 204
    _fail(ui, 4)
    assert login(ui).status_code == 204


def test_failures_expire_after_window(ui, clock):
    _fail(ui, 5)
    clock.advance(901)
    _fail(ui, 4)
    assert login(ui).status_code == 204


def test_per_ip_limit_does_not_block_other_ips(make_ui, db_path):
    a = make_ui(client=("10.0.0.1", 1))
    _fail(a, 5)
    assert login(a).status_code == 429
    # 同一個 app 的另一個來源
    b = TestClient(a.app, base_url="https://testserver", client=("10.0.0.2", 1))
    assert login(b).status_code == 204


def test_global_limit_blocks_all_sources(ui):
    app = ui.app
    for i in range(20):
        c = TestClient(app, base_url="https://testserver", client=(f"10.1.0.{i}", 1))
        _fail(c, 1)
    fresh = TestClient(app, base_url="https://testserver", client=("10.2.0.1", 1))
    assert login(fresh).status_code == 429
    # Bearer 路徑不受 UI 登入限流影響
    assert fresh.post("/v1/status", headers=AUTH).status_code == 200


def test_cf_connecting_ip_trusted_only_from_trusted_proxy(make_ui):
    trusted = make_ui(trusted_proxies="10.0.0.0/8", client=("10.0.0.1", 1))
    _fail(trusted, 5, headers={**UI, "CF-Connecting-IP": "203.0.113.5"})
    assert (
        login(trusted, headers={**UI, "CF-Connecting-IP": "203.0.113.5"}).status_code
        == 429
    )
    # 同一代理轉來的另一個真實來源不受影響
    assert (
        login(trusted, headers={**UI, "CF-Connecting-IP": "203.0.113.6"}).status_code
        == 204
    )


def test_cf_connecting_ip_ignored_from_untrusted_peer(make_ui):
    c = make_ui(trusted_proxies="192.168.0.0/16", client=("10.0.0.1", 1))
    _fail(c, 5, headers={**UI, "CF-Connecting-IP": "203.0.113.5"})
    # 偽造不同的 CF 標頭也換不到新的計數桶
    assert (
        login(c, headers={**UI, "CF-Connecting-IP": "203.0.113.99"}).status_code == 429
    )


def test_invalid_trusted_proxy_refuses_to_start(db_path):
    config = Config(ui=UiConfig(trusted_proxies="10.0.0.0/8, not-an-ip"))
    with pytest.raises(ConfigError, match="trusted_proxies"):
        create_app(make_settings(db_path, config=config))


# ── /ui 靜態檔與安全標頭 ─────────────────────────────────────────────


def test_static_ui_is_public_with_spa_fallback(ui):
    resp = ui.get("/ui", follow_redirects=False)
    assert resp.status_code in (301, 307)
    assert resp.headers["location"].endswith("/ui/")
    assert ui.get("/ui").status_code == 200
    index = ui.get("/ui/")
    assert index.status_code == 200
    assert '<div id="app">' in index.text
    deep = ui.get("/ui/notes/abc")  # 前端路由
    assert deep.status_code == 200
    assert deep.text == index.text
    asset = ui.get("/ui/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"


def test_missing_asset_and_unknown_ui_api_are_404_not_index(ui):
    assert ui.get("/ui/assets/missing.js").status_code == 404
    resp = ui.get("/ui/api/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_static_dir_without_index_refuses_to_start(db_path, tmp_path):
    config = Config(ui=UiConfig(static_dir=str(tmp_path / "empty")))
    with pytest.raises(ConfigError, match="index.html"):
        create_app(make_settings(db_path, config=config))


def test_without_static_dir_ui_api_still_works(make_ui):
    c = make_ui(static_dir=None)
    assert c.get("/ui/").status_code == 404
    assert login(c).status_code == 204


@pytest.mark.parametrize("path", ["/ui/", "/ui/notes", "/ui/assets/app.js"])
def test_security_headers_on_ui(ui, path):
    resp = ui.get(path)
    csp = resp.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'none'",
    ):
        assert directive in csp
    assert "unsafe-inline" not in csp
    assert "googleapis" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"


def test_ui_api_responses_are_not_cached(ui):
    for resp in (login(ui, key="nope-nope-nope-nope"), login(ui)):
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-content-type-options"] == "nosniff"


def test_index_is_revalidated_assets_are_not_forced(ui):
    assert ui.get("/ui/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in ui.get("/ui/assets/app.js").headers


def test_ui_routes_not_in_openapi(ui):
    paths = ui.get("/v1/openapi.json", headers=AUTH).json()["paths"]
    assert not any(p.startswith("/ui") for p in paths)


def test_ui_config_loads_from_env(tmp_path):
    from lore_vault.config import load_config

    config = load_config(
        environ={
            "LORE_VAULT_UI_COOKIE_SECURE": "false",
            "LORE_VAULT_UI_SESSION_IDLE_MINUTES": "15",
            "LORE_VAULT_UI_TRUSTED_PROXIES": "172.16.0.0/12",
        }
    )
    assert config.ui.cookie_secure is False
    assert config.ui.session_idle_minutes == 15.0
    assert config.ui.trusted_proxies == "172.16.0.0/12"


@pytest.mark.parametrize(
    "field", ["session_idle_minutes", "login_max_failures_per_ip", "max_sessions"]
)
def test_ui_config_rejects_non_positive(field):
    from lore_vault.config import load_config

    with pytest.raises(ConfigError, match=f"ui.{field}"):
        load_config(environ={f"LORE_VAULT_UI_{field.upper()}": "0"})


def test_simplecookie_parses_login_cookie(ui):
    """瀏覽器層面的健全性：Set-Cookie 可被標準解析器解析、值不含需跳脫字元。"""
    resp = login(ui)
    jar = SimpleCookie()
    jar.load(resp.headers["set-cookie"])
    assert SECURE_COOKIE in jar
