"""UI 帳號密碼登入與全域鎖定（A23）：帳號、失敗計數、鎖定、登入紀錄的服務層。

規則：
- 帳號存在 `ui_accounts`；username 即 session 的 principal（如 `UEPBernie`，與
  Eternity 帳號一致），display 是前端署名。儲存保留原大小寫，比對不分大小寫
  密碼只存 `hashlib.scrypt` 雜湊（隨機 salt，參數寫在列中）。密碼只經
  `cli.admin ui-set-password` 以 getpass 互動輸入，不從參數或環境變數讀
- 失敗計數是**全域**的（不分來源 IP）：累計 `MAX_FAILURES` 次即鎖定；鎖定後所有
  登入一律拒絕（含正確密碼，而且不再做密碼比對），只能人工 `unlock` 解除
- 未鎖定時，失敗計數在 Asia/Taipei 換日後歸零（惰性：下次嘗試或查詢時依日期判斷）。
  鎖定不因換日解除。登入成功**不**歸零計數（只有換日與人工解鎖會歸零）
- 狀態存在 DB：服務重啟不解鎖、不歸零
- 每次嘗試記一列 `ui_login_log`（時間、來源 IP、嘗試的 username、結果），不記密碼；
  超過保留天數的紀錄在服務啟動與每次嘗試時清除

呼叫端（HTTP 登入端點）必須把一次嘗試序列化（同一時間只跑一個 `attempt_login`）；
這裡另在寫入交易內重讀狀態，並行時也不會讓鎖定後的比對結果生效。
查詢與解鎖（`lock_status`／`unlock`／`login_log`）供 CLI 與日後的 HTTP 管理端點共用。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .checks import Reconciliation
from .db import transaction
from .timeutil import format_utc

# 台灣自 1979 年起無日光節約時間；固定 +08:00 即 Asia/Taipei。
# 不用 zoneinfo：Windows 上沒有系統時區資料庫，得多裝 tzdata 套件
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")

MAX_FAILURES = 3
MIN_PASSWORD_LENGTH = 12
# 密碼上限：擋掉拿超長輸入耗 CPU（scrypt 會先對密碼做 PBKDF2）
MAX_PASSWORD_LENGTH = 1024
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# 紀錄裡「嘗試的 username」是未認證輸入：去控制字元並截斷
LOG_USERNAME_MAX = 64
DISPLAY_MAX = 100
DEFAULT_RETENTION_DAYS = 90.0

RESULTS = ("success", "bad_credentials", "locked", "no_account", "unlock")


class AccountError(ValueError):
    """帳號或密碼設定不合規則（訊息不含密碼）。"""


@dataclass(frozen=True)
class ScryptParams:
    n: int = 2**15
    r: int = 8
    p: int = 1
    dklen: int = 32

    @property
    def maxmem(self) -> int:
        # scrypt 約需 128·r·n 位元組；Python 預設上限 32MiB 剛好卡在 n=2**15、r=8
        return 2 * 128 * self.r * self.n * self.p + 1024 * 1024


DEFAULT_PARAMS = ScryptParams()


def hash_password(
    password: str, *, params: ScryptParams = DEFAULT_PARAMS, salt: bytes | None = None
) -> tuple[bytes, bytes]:
    """回傳 (雜湊, salt)。"""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=params.maxmem,
        dklen=params.dklen,
    )
    return digest, salt


def verify_password(
    password: str, digest: bytes, salt: bytes, params: ScryptParams
) -> bool:
    candidate, _ = hash_password(password, params=params, salt=salt)
    return hmac.compare_digest(candidate, digest)


# 找不到帳號時也跑一次同參數的比對，回應時間不洩漏帳號是否存在
_DUMMY_SALT = secrets.token_bytes(16)


def _dummy_verify(password: str) -> None:
    hash_password(password[:MAX_PASSWORD_LENGTH], salt=_DUMMY_SALT)


# ── 帳號 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Account:
    username: str
    display: str
    created: str
    updated: str

    def to_dict(self) -> dict[str, str]:
        return {
            "username": self.username,
            "display": self.display,
            "created": self.created,
            "updated": self.updated,
        }


def validate_username(username: str) -> str:
    if not USERNAME_PATTERN.fullmatch(username):
        raise AccountError(
            f"username 必須是 1～64 個英數字或 . _ -（開頭為英數字），得到 {username!r}"
        )
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"密碼至少 {MIN_PASSWORD_LENGTH} 個字元")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"密碼不可超過 {MAX_PASSWORD_LENGTH} 個字元")


def validate_display(display: str) -> str:
    value = display.strip()
    if not value or len(value) > DISPLAY_MAX:
        raise AccountError(f"顯示名稱必須是 1～{DISPLAY_MAX} 個字元")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise AccountError("顯示名稱不可包含控制字元")
    return value


def _account(row: sqlite3.Row) -> Account:
    return Account(row["username"], row["display"], row["created"], row["updated"])


def get_account(conn: sqlite3.Connection, username: str) -> Account | None:
    row = conn.execute(
        "SELECT username, display, created, updated FROM ui_accounts "
        "WHERE username = ?",
        (username,),
    ).fetchone()
    return _account(row) if row is not None else None


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute(
        "SELECT username, display, created, updated FROM ui_accounts ORDER BY username"
    ).fetchall()
    return [_account(r) for r in rows]


def has_accounts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM ui_accounts LIMIT 1").fetchone() is not None


def set_password(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    now: datetime,
    display: str | None = None,
    params: ScryptParams = DEFAULT_PARAMS,
) -> tuple[Account, bool]:
    """建立帳號或更新密碼；回傳 (帳號, 是否新建)。

    username 比對不分大小寫：已有同名（大小寫不同）帳號時更新它，保留原本的大小寫
    （principal 不因打字大小寫改變）。新帳號未給 display 時用 username；既有帳號未給
    display 時保留原值。
    不動鎖定狀態（改密碼不等於解鎖）。
    """
    validate_username(username)
    validate_password(password)
    new_display = validate_display(display) if display is not None else None
    digest, salt = hash_password(password, params=params)
    stamp = format_utc(now)
    with transaction(conn):
        existing = get_account(conn, username)
        if existing is None:
            conn.execute(
                "INSERT INTO ui_accounts (username, display, password_hash, salt, "
                "scrypt_n, scrypt_r, scrypt_p, dklen, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    username,
                    new_display or username,
                    digest,
                    salt,
                    params.n,
                    params.r,
                    params.p,
                    params.dklen,
                    stamp,
                    stamp,
                ),
            )
        else:
            conn.execute(
                "UPDATE ui_accounts SET display = ?, password_hash = ?, salt = ?, "
                "scrypt_n = ?, scrypt_r = ?, scrypt_p = ?, dklen = ?, updated = ? "
                "WHERE username = ?",
                (
                    new_display or existing.display,
                    digest,
                    salt,
                    params.n,
                    params.r,
                    params.p,
                    params.dklen,
                    stamp,
                    username,
                ),
            )
        account = get_account(conn, username)
    assert account is not None
    return account, existing is None


# ── 鎖定狀態 ────────────────────────────────────────────────────────


def taipei_day(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).date().isoformat()


@dataclass(frozen=True)
class LockStatus:
    locked: bool
    locked_at: str | None
    # 目前有效的失敗次數（未鎖定且已換日時為 0）
    failures: int
    failure_day: str | None

    @property
    def remaining(self) -> int:
        return 0 if self.locked else max(0, MAX_FAILURES - self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "locked": self.locked,
            "locked_at": self.locked_at,
            "failures": self.failures,
            "failure_day": self.failure_day,
            "remaining": self.remaining,
            "max_failures": MAX_FAILURES,
        }


def _status(conn: sqlite3.Connection, now: datetime) -> LockStatus:
    row = conn.execute(
        "SELECT failures, failure_day, locked_at FROM ui_login_state WHERE id = 1"
    ).fetchone()
    if row is None:
        # 遷移會建立這一列；缺了代表有人手動刪除，視為鎖定（fail closed）
        return LockStatus(True, None, MAX_FAILURES, None)
    locked_at = row["locked_at"]
    failures = int(row["failures"])
    day = row["failure_day"]
    if locked_at is None and day != taipei_day(now):
        failures, day = 0, None
    return LockStatus(locked_at is not None, locked_at, failures, day)


def lock_status(conn: sqlite3.Connection, now: datetime) -> LockStatus:
    """目前的鎖定狀態（唯讀；換日歸零只反映在回傳值，下次嘗試才寫回）。"""
    return _status(conn, now)


def unlock(
    conn: sqlite3.Connection, *, now: datetime, source: str = "cli"
) -> dict[str, Any]:
    """人工解鎖並歸零失敗計數，寫一筆 `unlock` 紀錄；回傳解鎖前的狀態。"""
    with transaction(conn):
        before = _status(conn, now)
        conn.execute(
            "INSERT OR REPLACE INTO ui_login_state "
            "(id, failures, failure_day, locked_at, updated) "
            "VALUES (1, 0, NULL, NULL, ?)",
            (format_utc(now),),
        )
        _log(conn, now, source, None, "unlock")
    return {"was_locked": before.locked, "before": before.to_dict()}


# ── 登入嘗試 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoginOutcome:
    # success／bad_credentials／locked／no_account
    result: str
    status: LockStatus
    account: Account | None = None

    @property
    def ok(self) -> bool:
        return self.result == "success"


def _sanitize(value: str) -> str:
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    return cleaned[:LOG_USERNAME_MAX]


def _log(
    conn: sqlite3.Connection,
    now: datetime,
    ip: str,
    username: str | None,
    result: str,
) -> None:
    conn.execute(
        "INSERT INTO ui_login_log (at, ip, username, result) VALUES (?, ?, ?, ?)",
        (format_utc(now), ip[:100], username, result),
    )


def purge_log(conn: sqlite3.Connection, *, now: datetime, retention_days: float) -> int:
    """刪除早於保留期限的登入紀錄；回傳刪除筆數。"""
    cutoff = format_utc(now - timedelta(days=retention_days))
    with transaction(conn):
        cur = conn.execute("DELETE FROM ui_login_log WHERE at < ?", (cutoff,))
    return int(cur.rowcount or 0)


def attempt_login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    ip: str,
    now: datetime,
    retention_days: float = DEFAULT_RETENTION_DAYS,
) -> LoginOutcome:
    """一次登入嘗試：鎖定中直接拒絕（不比對密碼）；否則比對並更新全域計數。"""
    attempted = _sanitize(username)
    lookup = username.strip()
    with transaction(conn):
        conn.execute(
            "DELETE FROM ui_login_log WHERE at < ?",
            (format_utc(now - timedelta(days=retention_days)),),
        )
        status = _status(conn, now)
        if status.locked:
            _log(conn, now, ip, attempted, "locked")
            return LoginOutcome("locked", status)
        if not has_accounts(conn):
            _log(conn, now, ip, attempted, "no_account")
            return LoginOutcome("no_account", status)
        row = conn.execute(
            "SELECT username, display, created, updated, password_hash, salt, "
            "scrypt_n, scrypt_r, scrypt_p, dklen FROM ui_accounts WHERE username = ?",
            (lookup,),
        ).fetchone()
    # scrypt 在交易外跑：不佔著寫鎖拖住其他寫入
    matched = False
    if row is None or len(password) > MAX_PASSWORD_LENGTH:
        _dummy_verify(password)
    else:
        params = ScryptParams(
            row["scrypt_n"], row["scrypt_r"], row["scrypt_p"], row["dklen"]
        )
        matched = verify_password(password, row["password_hash"], row["salt"], params)
    with transaction(conn):
        # 重讀：比對期間可能已被其他嘗試鎖定
        status = _status(conn, now)
        if status.locked:
            _log(conn, now, ip, attempted, "locked")
            return LoginOutcome("locked", status)
        if matched:
            assert row is not None
            _log(conn, now, ip, attempted, "success")
            return LoginOutcome("success", status, _account(row))
        failures = status.failures + 1
        locked_at = format_utc(now) if failures >= MAX_FAILURES else None
        day = taipei_day(now)
        conn.execute(
            "UPDATE ui_login_state SET failures = ?, failure_day = ?, locked_at = ?, "
            "updated = ? WHERE id = 1",
            (failures, day, locked_at, format_utc(now)),
        )
        _log(conn, now, ip, attempted, "bad_credentials")
    return LoginOutcome(
        "bad_credentials", LockStatus(locked_at is not None, locked_at, failures, day)
    )


# ── 查詢 ────────────────────────────────────────────────────────────


def login_log(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """最近的登入紀錄（新到舊）。"""
    rows = conn.execute(
        "SELECT at, ip, username, result FROM ui_login_log ORDER BY seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def lock_check(conn: sqlite3.Connection, *, now: datetime) -> Reconciliation:
    """doctor：鎖定中為 fail（附鎖定時間）；近 24 小時失敗次數為資訊；無帳號為 warn。"""
    status = _status(conn, now)
    since = format_utc(now - timedelta(hours=24))
    counts = {
        row["result"]: int(row["n"])
        for row in conn.execute(
            "SELECT result, count(*) AS n FROM ui_login_log WHERE at >= ? "
            "GROUP BY result",
            (since,),
        )
    }
    accounts = int(conn.execute("SELECT count(*) FROM ui_accounts").fetchone()[0])
    data = {
        "accounts": accounts,
        "failures": status.failures,
        "failures_24h": counts.get("bad_credentials", 0),
        "locked_attempts_24h": counts.get("locked", 0),
        "success_24h": counts.get("success", 0),
    }
    tail = f"近 24 小時失敗 {data['failures_24h']} 次"
    if status.locked:
        return Reconciliation(
            "fail",
            f"UI 登入已鎖定（自 {status.locked_at or '不明'}）；{tail}。"
            "確認後以 `python -m lore_vault.cli.admin ui-unlock --yes` 解鎖",
            data,
        )
    if accounts == 0:
        return Reconciliation(
            "warn",
            "尚未設定 UI 帳號（`python -m lore_vault.cli.admin ui-set-password "
            f"--user <名稱>`）；{tail}",
            data,
        )
    return Reconciliation(
        "pass",
        f"未鎖定，目前失敗 {status.failures}/{MAX_FAILURES}；{tail}",
        data,
    )
