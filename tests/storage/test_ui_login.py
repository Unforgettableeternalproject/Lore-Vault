"""A23：UI 帳號密碼（scrypt）、全域失敗計數與鎖定、登入紀錄，
以及 doctor `ui.login_lock`。

HTTP 層的行為（423／剩餘次數／重啟）見 tests/api/test_ui_auth.py；這裡測服務層本身。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lore_vault.doctor import DoctorContext, Status, default_registry
from lore_vault.storage import ui_login
from lore_vault.storage.db import connect, connect_readonly

CHEAP = ui_login.ScryptParams(n=2**10)
# 台北 2026-09-26 23:00
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"


@pytest.fixture
def account(conn):
    ui_login.set_password(
        conn, "UEPBernie", PASSWORD, now=NOW, display="Xavier (Bernie)", params=CHEAP
    )
    return conn


def _try(conn, password: str, *, now=NOW, username: str = "UEPBernie"):
    return ui_login.attempt_login(conn, username, password, ip="10.0.0.1", now=now)


# ── 雜湊 ────────────────────────────────────────────────────────────


def test_default_scrypt_roundtrip_and_random_salt():
    """預設參數（n=2**15、r=8）要能在 maxmem 內算出；同密碼兩次 salt 不同。"""
    digest, salt = ui_login.hash_password(PASSWORD)
    params = ui_login.DEFAULT_PARAMS
    assert ui_login.verify_password(PASSWORD, digest, salt, params)
    assert not ui_login.verify_password(PASSWORD + "x", digest, salt, params)
    again, salt2 = ui_login.hash_password(PASSWORD)
    assert salt != salt2 and digest != again
    assert len(digest) == params.dklen


def test_hash_and_params_stored_without_plaintext(account):
    row = account.execute(
        "SELECT username, display, password_hash, salt, scrypt_n, scrypt_r, scrypt_p, "
        "dklen FROM ui_accounts"
    ).fetchone()
    assert (row["username"], row["display"]) == ("UEPBernie", "Xavier (Bernie)")
    assert (row["scrypt_n"], row["scrypt_r"], row["scrypt_p"], row["dklen"]) == (
        2**10,
        8,
        1,
        32,
    )
    assert PASSWORD.encode() not in bytes(row["password_hash"])
    assert ui_login.verify_password(PASSWORD, row["password_hash"], row["salt"], CHEAP)


@pytest.mark.parametrize(
    ("username", "password", "display"),
    [
        ("UEPBernie", "short", None),
        ("bad name", PASSWORD, None),
        ("-lead", PASSWORD, None),
        ("x" * 65, PASSWORD, None),
        ("UEPBernie", PASSWORD, "  "),
        ("UEPBernie", PASSWORD, "a\nb"),
    ],
)
def test_set_password_validation(conn, username, password, display):
    with pytest.raises(ui_login.AccountError):
        ui_login.set_password(
            conn, username, password, now=NOW, display=display, params=CHEAP
        )
    assert not ui_login.has_accounts(conn)


def test_username_case_preserved_and_matched_case_insensitively(account):
    # 更新密碼時打小寫：更新同一帳號、保留原大小寫與顯示名稱
    updated, created = ui_login.set_password(
        account, "uepbernie", "another password!", now=NOW, params=CHEAP
    )
    assert created is False
    assert (updated.username, updated.display) == ("UEPBernie", "Xavier (Bernie)")
    assert len(ui_login.list_accounts(account)) == 1
    outcome = _try(account, "another password!", username="uepBERNIE")
    assert outcome.ok and outcome.account.username == "UEPBernie"


def test_set_password_does_not_unlock(account):
    for _ in range(3):
        _try(account, "wrong-wrong-wrong")
    ui_login.set_password(account, "UEPBernie", "brand new password", now=NOW)
    assert ui_login.lock_status(account, NOW).locked
    assert _try(account, "brand new password").result == "locked"


# ── 計數與鎖定 ──────────────────────────────────────────────────────


def test_remaining_counts_down_then_locks(account):
    results = [_try(account, "nope-nope-nope") for _ in range(3)]
    assert [r.result for r in results] == ["bad_credentials"] * 3
    assert [r.status.remaining for r in results] == [2, 1, 0]
    assert [r.status.locked for r in results] == [False, False, True]
    assert results[2].status.locked_at == "2026-09-26T15:00:00.000Z"
    assert _try(account, PASSWORD).result == "locked"


def test_daily_reset_uses_taipei_date(account):
    _try(account, "nope-nope-nope")
    _try(account, "nope-nope-nope")
    # UTC 15:59 = 台北 23:59：同一天
    assert ui_login.lock_status(account, NOW + timedelta(minutes=59)).remaining == 1
    # UTC 16:00 = 台北隔天 00:00
    after = NOW + timedelta(hours=1)
    assert ui_login.lock_status(account, after).remaining == 3
    assert _try(account, "nope-nope-nope", now=after).status.remaining == 2


def test_no_account_does_not_count(conn):
    outcome = _try(conn, PASSWORD)
    assert outcome.result == "no_account"
    assert ui_login.lock_status(conn, NOW).failures == 0


def test_unlock_resets_and_logs(account):
    for _ in range(3):
        _try(account, "nope-nope-nope")
    result = ui_login.unlock(account, now=NOW)
    assert result["was_locked"] is True and result["before"]["failures"] == 3
    status = ui_login.lock_status(account, NOW)
    assert (status.locked, status.failures, status.remaining) == (False, 0, 3)
    log = ui_login.login_log(account, limit=2)
    assert log[0] == {
        "at": "2026-09-26T15:00:00.000Z",
        "ip": "cli",
        "username": None,
        "result": "unlock",
    }
    assert _try(account, PASSWORD).ok


def test_log_username_is_sanitized_and_truncated(account):
    _try(account, "nope-nope-nope", username="evil\x00\n" + "a" * 100)
    entry = ui_login.login_log(account, limit=1)[0]
    assert entry["username"] == "evil" + "a" * 60
    assert entry["result"] == "bad_credentials"


def test_purge_log(account):
    _try(account, PASSWORD)
    assert ui_login.purge_log(account, now=NOW, retention_days=1) == 0
    later = NOW + timedelta(days=2)
    assert ui_login.purge_log(account, now=later, retention_days=1) == 1
    assert ui_login.login_log(account) == []


# ── doctor ──────────────────────────────────────────────────────────


def _doctor(db_path, now=NOW):
    conn = connect_readonly(db_path)
    try:
        report = default_registry().run(
            DoctorContext(settings={"now": now}, resources={"db": conn}),
            categories=["ui"],
        )
    finally:
        conn.close()
    (outcome,) = report.outcomes
    assert outcome.name == "ui.login_lock"
    return outcome.result


def test_doctor_fails_while_locked_with_lock_time(account, db_path):
    assert _doctor(db_path).status is Status.PASS
    for _ in range(3):
        _try(account, "nope-nope-nope")
    result = _doctor(db_path)
    assert result.status is Status.FAIL
    assert "2026-09-26T15:00:00.000Z" in result.summary
    assert "ui-unlock" in result.summary
    assert result.counts["failures_24h"] == 3
    # 鎖定不因換日消失
    assert _doctor(db_path, NOW + timedelta(days=2)).status is Status.FAIL
    ui_login.unlock(account, now=NOW)
    assert _doctor(db_path).status is Status.PASS


def test_doctor_reports_failures_in_last_24h(account, db_path):
    _try(account, "nope-nope-nope")
    result = _doctor(db_path)
    assert result.status is Status.PASS
    assert result.counts["failures_24h"] == 1 and result.counts["failures"] == 1
    later = _doctor(db_path, NOW + timedelta(hours=25))
    assert later.counts["failures_24h"] == 0 and later.counts["failures"] == 0


def test_doctor_warns_without_account(conn, db_path):
    result = _doctor(db_path)
    assert result.status is Status.WARN
    assert "ui-set-password" in result.summary


def test_doctor_skips_before_v13(tmp_path):
    import sqlite3

    from lore_vault.storage import migrate as migrate_mod

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        migrate_mod.migrate(raw, migrations=migrate_mod.MIGRATIONS[:12])
    finally:
        raw.close()
    assert _doctor(path).status is Status.SKIPPED
    # 確認 fixture 本身沒被遷移到 v13
    conn = connect(path, run_migrations=False)
    try:
        assert migrate_mod.current_version(conn) == 12
    finally:
        conn.close()
