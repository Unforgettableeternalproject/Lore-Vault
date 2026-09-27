"""D13：episode 收料開關（預設關閉）與執行期設定 API（UI session 限定、稽核、
修改後不重建 app 立即生效）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lore_vault.api.app import create_app
from lore_vault.config import (
    Config,
    EmbeddingConfig,
    EpisodesConfig,
    UiConfig,
    load_config,
)
from lore_vault.doctor import Check, CheckResult, Registry
from lore_vault.runtime_settings import SPECS
from lore_vault.storage.db import connect
from lore_vault.storage.timeutil import format_utc

from .conftest import (
    AUTH,
    DIM,
    UI_DISPLAY,
    UI_LOGIN,
    UI_USER,
    SpaceClient,
    create_vault,
    embed_all,
    make_settings,
    seed_ui_account,
    write_note,
)
from .test_ask_http import FakeAnswerer, answer
from .test_spike_endpoints import episode

UI = {"X-Lore-Vault-UI": "1"}
VAULT = "folder/settings-demo"


def _episode(n: int = 0) -> dict:
    return episode(prompt_id=f"p-{n}", turn_index=n, vault=VAULT)


def _config(**sections) -> Config:
    return Config(
        embedding=EmbeddingConfig(dim=DIM), ui=UiConfig(static_dir=None), **sections
    )


@pytest.fixture
def open_client(db_path):
    """(bearer client, UI session client) 共用同一個 app；離開時關閉。"""
    opened: list[TestClient] = []

    def make(config: Config | None = None, **overrides):
        seed_ui_account(db_path)
        app = create_app(
            make_settings(db_path, config=config or _config(), **overrides)
        )
        bearer = SpaceClient(app)
        bearer.__enter__()
        bearer.headers.update(AUTH)
        ui = TestClient(app, base_url="https://testserver")
        ui.__enter__()
        resp = ui.post("/ui/api/login", json=UI_LOGIN, headers=UI)
        assert resp.status_code == 204, resp.text
        ui.headers.update(UI)
        opened.extend([ui, bearer])
        return bearer, ui

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    return body


def _item(data: dict, key: str) -> dict:
    return next(i for i in data["items"] if i["key"] == key)


def _set(ui: TestClient, **values) -> dict:
    resp = ui.post(
        "/v1/settings_update",
        json={"values": {k.replace("__", "."): v for k, v in values.items()}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 設定載入：預設關閉 ──


def test_episode_ingest_defaults_off_and_env_turns_it_on():
    assert Config().episodes.ingest is False
    assert load_config(environ={}).episodes.ingest is False
    on = load_config(environ={"LORE_VAULT_EPISODES_INGEST": "true"})
    assert on.episodes.ingest is True
    assert load_config(environ={}).ask.enabled is True


# ── episode 收料開關 ──


def test_post_episodes_rejected_when_ingest_off_but_reads_still_work(open_client):
    on_bearer, _ = open_client(_config(episodes=EpisodesConfig(ingest=True)))
    assert (
        on_bearer.post("/v1/episodes", json={"episodes": [_episode(0)]}).json()[
            "accepted"
        ]
        == 1
    )
    bearer, _ = open_client(_config())
    body = _error(
        bearer.post("/v1/episodes", json={"episodes": [_episode(1)]}),
        403,
        "episode_ingest_disabled",
    )
    assert "episodes.ingest" in body["error"]["message"]
    # 拒收不寫任何東西；已收進來的照常可讀
    page = bearer.get("/v1/episodes", params={"vault": "*"}).json()
    assert [e["prompt_id"] for e in page["items"]] == ["p-0"]
    summary = bearer.post(
        "/v1/episode_summary", json={"space": "dev", "vault": "*"}
    ).json()
    assert summary["total"] == 1


def test_ingest_toggle_takes_effect_without_rebuilding_app(open_client, db_path):
    bearer, ui = open_client()

    def post(n: int):
        return bearer.post("/v1/episodes", json={"episodes": [_episode(n)]})

    _error(post(0), 403, "episode_ingest_disabled")
    data = _set(ui, episodes__ingest=True)
    item = _item(data, "episodes.ingest")
    assert (item["value"], item["default"], item["source"]) == (True, False, "override")
    assert item["override"]["updated_by"] == UI_USER
    assert post(0).json()["accepted"] == 1
    # 還原預設 → 立即又拒收
    resp = ui.post("/v1/settings_reset", json={"keys": ["episodes.ingest"]})
    assert resp.status_code == 200, resp.text
    assert _item(resp.json(), "episodes.ingest")["source"] == "default"
    _error(post(1), 403, "episode_ingest_disabled")


def test_status_skips_ingest_recency_when_off(open_client):
    bearer, ui = open_client()

    def check() -> dict:
        data = bearer.post("/v1/status").json()
        return next(
            c
            for c in data["doctor"]["checks"]
            if c["name"] == "episodes.ingest_recency"
        )

    assert check()["status"] == "skipped"
    _set(ui, episodes__ingest=True)
    # 打開後照常檢查（尚無收料為 warn）
    assert check()["status"] == "warn"


# ── 認證：只允許 UI session ──


def test_settings_endpoints_reject_bearer_before_body_validation(open_client):
    bearer, _ = open_client()
    _error(bearer.get("/v1/settings"), 403, "ui_session_required")
    _error(
        bearer.post("/v1/settings_update", json={"values": {"episodes.ingest": True}}),
        403,
        "ui_session_required",
    )
    # 壞 body 也是 403（不洩漏 422 細節）
    _error(
        bearer.post("/v1/settings_update", json={"x": 1}), 403, "ui_session_required"
    )
    _error(
        bearer.post("/v1/settings_reset", json={"keys": ["episodes.ingest"]}),
        403,
        "ui_session_required",
    )


def test_settings_require_csrf_header_and_login(db_path):
    seed_ui_account(db_path)
    app = create_app(make_settings(db_path, config=_config()))
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/v1/settings", headers=UI).status_code == 401
        assert c.post("/ui/api/login", json=UI_LOGIN, headers=UI).status_code == 204
        _error(c.get("/v1/settings"), 403, "csrf_required")
        assert c.get("/v1/settings", headers=UI).status_code == 200


# ── 讀取、驗證、稽核 ──


def test_get_lists_whitelist_with_defaults_and_categories(open_client):
    _, ui = open_client(_config(episodes=EpisodesConfig(ingest=True)))
    data = ui.get("/v1/settings").json()
    assert [i["key"] for i in data["items"]] == [s.key for s in SPECS]
    assert {c["id"] for c in data["categories"]} >= {
        i["category"] for i in data["items"]
    }
    ingest = _item(data, "episodes.ingest")
    # 設定檔／環境變數的值就是「預設值」
    assert (ingest["value"], ingest["default"], ingest["source"]) == (
        True,
        True,
        "default",
    )
    assert ingest["override"] is None and ingest["type"] == "bool"
    snippet = _item(data, "ask.snippet_max_chars")
    assert (snippet["type"], snippet["min"], snippet["max"]) == ("int", 500, 50000)
    assert data["invalid_overrides"] == [] and data["audit"] == []
    # 密鑰、路徑與需重啟的設定不在白名單
    keys = {i["key"] for i in data["items"]}
    for forbidden in ("database.path", "ask.model", "worker.batch_size"):
        assert forbidden not in keys


@pytest.mark.parametrize(
    "values, key, code",
    [
        ({"episodes.ingest": "true"}, "episodes.ingest", "invalid_value"),
        ({"episodes.ingest": 1}, "episodes.ingest", "invalid_value"),
        ({"ask.snippet_max_chars": 100}, "ask.snippet_max_chars", "invalid_value"),
        ({"ask.snippet_max_chars": 1000.5}, "ask.snippet_max_chars", "invalid_value"),
        ({"backup.max_age_hours": True}, "backup.max_age_hours", "invalid_value"),
        ({"database.path": "/tmp/x.db"}, "database.path", "unknown_setting"),
    ],
)
def test_invalid_values_rejected_per_key(open_client, values, key, code):
    _, ui = open_client()
    body = _error(
        ui.post("/v1/settings_update", json={"values": values}), 400, "invalid_setting"
    )
    assert [(e["key"], e["code"]) for e in body["error"]["errors"]] == [(key, code)]


def test_batch_is_all_or_nothing(open_client, db_path):
    _, ui = open_client()
    body = _error(
        ui.post(
            "/v1/settings_update",
            json={"values": {"episodes.ingest": True, "ask.snippet_max_chars": -1}},
        ),
        400,
        "invalid_setting",
    )
    assert [e["key"] for e in body["error"]["errors"]] == ["ask.snippet_max_chars"]
    data = ui.get("/v1/settings").json()
    assert _item(data, "episodes.ingest")["source"] == "default"
    assert data["audit"] == []
    _error(
        ui.post("/v1/settings_reset", json={"keys": ["nope.x"]}), 400, "invalid_setting"
    )
    assert ui.post("/v1/settings_update", json={"values": {}}).status_code == 422


def test_audit_records_who_when_old_and_new(open_client, db_path):
    _, ui = open_client()
    data = _set(ui, ask__snippet_max_chars=3000, episodes__ingest=True)
    assert {e["key"] for e in data["changed"]} == {
        "ask.snippet_max_chars",
        "episodes.ingest",
    }
    snippet = next(e for e in data["changed"] if e["key"] == "ask.snippet_max_chars")
    assert (snippet["action"], snippet["old_value"], snippet["new_value"]) == (
        "set",
        6000,
        3000,
    )
    assert (snippet["principal"], snippet["display"]) == (UI_USER, UI_DISPLAY)
    datetime.fromisoformat(snippet["at"])
    # 同值再存一次：不寫、不記稽核
    assert _set(ui, ask__snippet_max_chars=3000)["changed"] == []
    reset = ui.post(
        "/v1/settings_reset", json={"keys": ["ask.snippet_max_chars"]}
    ).json()
    (entry,) = reset["changed"]
    assert (entry["action"], entry["old_value"], entry["new_value"]) == (
        "reset",
        3000,
        6000,
    )
    # 沒有覆寫的鍵還原：不記
    again = ui.post(
        "/v1/settings_reset", json={"keys": ["ask.snippet_max_chars"]}
    ).json()
    assert again["changed"] == []
    assert [e["action"] for e in reset["audit"]][:1] == ["reset"]
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, action FROM settings_audit ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [
        ("ask.snippet_max_chars", "set"),
        ("episodes.ingest", "set"),
        ("ask.snippet_max_chars", "reset"),
    ]


def test_doctor_settings_checks_pass_after_api_changes(open_client):
    bearer, ui = open_client()
    _set(ui, ask__enabled=False, episodes__ingest=True)
    ui.post("/v1/settings_reset", json={"keys": ["ask.enabled"]})
    checks = {
        c["name"]: c["status"]
        for c in bearer.post("/v1/status").json()["doctor"]["checks"]
    }
    assert checks["settings.overrides"] == "pass"
    assert checks["settings.audit_agreement"] == "pass"


# ── 各項即時生效 ──


def test_ask_toggle_and_snippet_limit_take_effect_immediately(open_client, db_path):
    fake = FakeAnswerer(*(answer("insufficient") for _ in range(3)))
    bearer, ui = open_client(answerer=fake)
    create_vault(bearer, VAULT)
    # 首段短（沒有 summary 時以首段當摘要），長段落只會出現在正文節錄
    tail = "甲" * 2000
    body = "SQLite 選型摘要。\n\n" + tail
    write_note(bearer, VAULT, "SQLite 選型", body)
    embed_all(db_path)

    def ask():
        return bearer.post("/v1/ask", json={"question": "SQLite 選型", "vault": VAULT})

    assert ask().status_code == 200
    assert tail in fake.calls[-1][1]
    _set(ui, ask__snippet_max_chars=500)
    assert ask().status_code == 200
    assert tail not in fake.calls[-1][1]
    _set(ui, ask__enabled=False)
    calls = len(fake.calls)
    _error(ask(), 403, "ask_disabled")
    assert len(fake.calls) == calls  # 不呼叫模型
    checks = {
        c["name"]: c["status"]
        for c in bearer.post("/v1/status").json()["doctor"]["checks"]
    }
    assert checks["ask.provider"] == "skipped"
    ui.post("/v1/settings_reset", json={"keys": ["ask.enabled"]})
    assert ask().status_code == 200


def test_status_passes_runtime_thresholds_to_doctor(open_client, monkeypatch):
    """四個門檻（備份、文件卡住、墓碑年齡／容量）由 /v1/status 讀執行期有效值。"""
    bearer, ui = open_client()
    seen: list[dict] = []

    def spy(ctx):
        seen.append(dict(ctx.settings))
        return CheckResult.ok()

    monkeypatch.setattr(
        "lore_vault.api.routes.default_registry",
        lambda: Registry([Check("spy.settings", "spy", spy, "spy")]),
    )
    bearer.post("/v1/status")
    before = seen[-1]
    assert before["backup_max_age_hours"] == 26.0
    _set(
        ui,
        backup__max_age_hours=2,
        documents__stuck_seconds=120,
        database__tombstone_warn_age_days=7,
        database__tombstone_warn_bytes=4096,
    )
    bearer.post("/v1/status")
    after = seen[-1]
    assert after["backup_max_age_hours"] == 2.0
    assert after["documents_stuck_seconds"] == 120.0
    assert after["tombstones_warn_age_days"] == 7.0
    assert after["tombstones_warn_bytes"] == 4096


def test_tombstone_threshold_override_changes_doctor_result(open_client, db_path):
    bearer, ui = open_client()
    conn = connect(db_path)
    try:
        old = format_utc(datetime.now(UTC) - timedelta(days=30))
        conn.execute(
            "INSERT INTO note_tombstones (note_id, vault, deleted_at, reason) "
            "VALUES ('n-gone', 'folder/x', ?, 'r')",
            (old,),
        )
    finally:
        conn.close()

    def status() -> str:
        checks = bearer.post("/v1/status").json()["doctor"]["checks"]
        return next(c for c in checks if c["name"] == "tombstones.summary")["status"]

    assert status() == "pass"
    _set(ui, database__tombstone_warn_age_days=7)
    assert status() == "warn"


def test_login_log_retention_override_applies_on_next_attempt(open_client, db_path):
    _, ui = open_client()
    conn = connect(db_path)
    try:
        old = format_utc(datetime.now(UTC) - timedelta(days=10))
        conn.execute(
            "INSERT INTO ui_login_log (at, ip, username, result) "
            "VALUES (?, 'x', 'u', 'success')",
            (old,),
        )
    finally:
        conn.close()

    def old_rows() -> int:
        c = connect(db_path)
        try:
            return c.execute(
                "SELECT count(*) FROM ui_login_log WHERE username = 'u'"
            ).fetchone()[0]
        finally:
            c.close()

    ui.post("/ui/api/login", json=UI_LOGIN)
    assert old_rows() == 1  # 預設保留 90 天
    _set(ui, ui__login_log_retention_days=5)
    ui.post("/ui/api/login", json=UI_LOGIN)
    assert old_rows() == 0


def test_http_mcp_download_limit_reads_runtime_value(open_client):
    from lore_vault.mcp.http import _runtime_download_limit

    bearer, ui = open_client()
    app = bearer.app
    config = app.state.lore.settings.config
    assert _runtime_download_limit(app, config) == 1024 * 1024
    _set(ui, mcp__http_download_max_bytes=2048)
    assert _runtime_download_limit(app, config) == 2048


def test_invalid_db_override_is_ignored_and_reported(open_client, db_path):
    bearer, ui = open_client()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settings_overrides (key, value, updated, updated_by) "
            "VALUES ('episodes.ingest', ?, '2026-09-27T00:00:00.000Z', 'x')",
            (json.dumps("yes"),),
        )
    finally:
        conn.close()
    bearer.app.state.lore.runtime.invalidate()
    _error(
        bearer.post("/v1/episodes", json={"episodes": [_episode(0)]}),
        403,
        "episode_ingest_disabled",
    )
    data = ui.get("/v1/settings").json()
    assert [o["key"] for o in data["invalid_overrides"]] == ["episodes.ingest"]
    assert _item(data, "episodes.ingest")["source"] == "default"
    checks = {
        c["name"]: c["status"]
        for c in bearer.post("/v1/status").json()["doctor"]["checks"]
    }
    assert checks["settings.overrides"] == "fail"
    assert checks["settings.audit_agreement"] == "fail"
    # 經 API 重設後恢復一致
    ui.post("/v1/settings_update", json={"values": {"episodes.ingest": True}})
    checks = {
        c["name"]: c["status"]
        for c in bearer.post("/v1/status").json()["doctor"]["checks"]
    }
    assert checks["settings.overrides"] == "pass"
    assert checks["settings.audit_agreement"] == "pass"
