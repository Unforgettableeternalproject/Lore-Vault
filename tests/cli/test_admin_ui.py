"""A23：`cli.admin` 的 UI 帳號與鎖定指令。密碼只能互動輸入（getpass、需要終端機），
不接受參數、環境變數或 pipe。"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from lore_vault.cli import admin as cli
from lore_vault.storage import ui_login
from lore_vault.storage.db import connect

PASSWORD = "correct horse battery"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "lore.db"
    connect(path).close()
    return path


@pytest.fixture
def tty(monkeypatch):
    """模擬互動終端機：getpass 依序回傳 `answers`。"""
    answers: list[str] = []
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": answers.pop(0))
    return answers


def _run(db, *args) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = cli.main(["--db", str(db), *args], stdout=out)
    return code, json.loads(out.getvalue()) if out.getvalue() else None


def _accounts(db) -> list[tuple]:
    conn = connect(db)
    try:
        return [
            tuple(r) for r in conn.execute("SELECT username, display FROM ui_accounts")
        ]
    finally:
        conn.close()


def test_set_password_interactive_creates_then_updates(db, tty):
    tty.extend([PASSWORD, PASSWORD])
    code, out = _run(
        db, "ui-set-password", "--user", "UEPBernie", "--display", "Xavier (Bernie)"
    )
    assert code == 0
    assert out["mode"] == "created"
    assert (out["username"], out["display"]) == ("UEPBernie", "Xavier (Bernie)")
    assert PASSWORD not in json.dumps(out)
    tty.extend(["another password!", "another password!"])
    code, out = _run(db, "ui-set-password", "--user", "uepbernie")
    assert code == 0 and out["mode"] == "updated"
    assert _accounts(db) == [("UEPBernie", "Xavier (Bernie)")]


def test_password_argument_is_rejected(db, tty):
    with pytest.raises(SystemExit) as exc:
        _run(db, "ui-set-password", "--user", "UEPBernie", "--password", PASSWORD)
    assert exc.value.code == 2
    assert _accounts(db) == []


def test_non_tty_is_rejected_even_with_env(db, monkeypatch, capsys):
    """pipe／agent 代跑（stdin 不是終端機）一律拒絕；環境變數不會被讀。"""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setenv("LORE_VAULT_UI_PASSWORD", PASSWORD)
    monkeypatch.setenv("PASSWORD", PASSWORD)

    def no_prompt(prompt=""):
        raise AssertionError("非終端機不應提示輸入")

    monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
    code, _ = _run(db, "ui-set-password", "--user", "UEPBernie")
    assert code == 1
    assert "docker exec -it" in capsys.readouterr().err
    assert _accounts(db) == []


@pytest.mark.parametrize(
    "answers",
    [["short", "short"], [PASSWORD, PASSWORD + "x"]],
    ids=["too_short", "mismatch"],
)
def test_bad_password_input_is_rejected(db, tty, answers, capsys):
    tty.extend(answers)
    code, _ = _run(db, "ui-set-password", "--user", "UEPBernie")
    assert code == 1
    err = capsys.readouterr().err
    assert PASSWORD not in err and "short" not in err
    assert _accounts(db) == []


def test_invalid_username_rejected_before_prompt(db, tty):
    code, _ = _run(db, "ui-set-password", "--user", "bad name")
    assert code == 1
    assert _accounts(db) == []


def test_lock_status_log_and_unlock(db):
    conn = connect(db)
    try:
        ui_login.set_password(
            conn,
            "UEPBernie",
            PASSWORD,
            now=datetime.now(UTC),
            params=ui_login.ScryptParams(n=2**10),
        )
        for _ in range(3):
            ui_login.attempt_login(
                conn, "UEPBernie", "nope-nope-nope", ip="1.2.3.4", now=datetime.now(UTC)
            )
    finally:
        conn.close()

    code, status = _run(db, "ui-lock-status")
    assert code == 0
    assert status["locked"] is True and status["remaining"] == 0
    assert status["accounts"][0]["username"] == "UEPBernie"
    assert "password_hash" not in json.dumps(status)

    code, log = _run(db, "ui-login-log", "--limit", "2")
    assert code == 0 and len(log["items"]) == 2
    assert log["items"][0]["ip"] == "1.2.3.4"
    assert set(log["items"][0]) == {"at", "ip", "username", "result"}

    code, dry = _run(db, "ui-unlock")
    assert code == 0 and dry["mode"] == "dry_run" and dry["locked"] is True
    assert _run(db, "ui-lock-status")[1]["locked"] is True  # dry-run 不解鎖

    code, done = _run(db, "ui-unlock", "--yes")
    assert code == 0 and done["mode"] == "unlocked" and done["was_locked"] is True
    status = _run(db, "ui-lock-status")[1]
    assert (status["locked"], status["failures"], status["remaining"]) == (
        False,
        0,
        3,
    )
    assert _run(db, "ui-login-log", "--limit", "1")[1]["items"][0]["result"] == "unlock"


def test_login_log_limit_must_be_positive(db):
    with pytest.raises(SystemExit) as exc:
        _run(db, "ui-login-log", "--limit", "0")
    assert exc.value.code == 2
