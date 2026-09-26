"""UI 的本地身分驗證（A21）：服務端 session、登入限流、用戶端 IP 判定。

- 登入金鑰就是 `LORE_VAULT_API_TOKEN`（不另設密碼，少一組要輪替的密鑰）
- session 只存在服務程序記憶體：重啟即全部失效（使用者重新登入即可），
  不寫資料庫、不留可被帶走的憑證。以 sha256(session id) 當鍵，記憶體裡沒有原值
- 期限：絕對期限（登入後固定時長）與閒置期限（最後一次認證請求起算），先到先失效
- 限流：同一來源 IP 與全域各自計算時間窗內的失敗次數；達上限後指數退避，
  退避期間連正確金鑰都回 429（否則猜測仍在進行）。成功只清除該 IP 的紀錄
- CSRF：cookie 路徑一律要求 `X-Lore-Vault-UI: 1` 標頭（跨來源請求帶自訂標頭必須
  先過 CORS preflight，本服務不回 CORS 標頭，所以瀏覽器送不出來），外加 SameSite=Strict
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from starlette.types import Scope

from lore_vault.config import ConfigError, UiConfig

UI_HEADER = b"x-lore-vault-ui"
UI_HEADER_VALUE = b"1"
CF_CONNECTING_IP = b"cf-connecting-ip"

Clock = Callable[[], float]
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def cookie_name(secure: bool) -> str:
    """Secure 時加 `__Host-` 前綴（瀏覽器限定本 host、Path=/、無 Domain）。"""
    return "__Host-lv_session" if secure else "lv_session"


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [v for k, v in scope.get("headers", ()) if k.lower() == name]


def has_ui_header(scope: Scope) -> bool:
    """CSRF 防護：恰好一個 `X-Lore-Vault-UI: 1`。"""
    values = _header_values(scope, UI_HEADER)
    return len(values) == 1 and values[0].strip() == UI_HEADER_VALUE


def read_cookie(scope: Scope, name: str) -> str | None:
    """從 Cookie 標頭取指定名稱的值；同名出現多次視為可疑，回 None。"""
    found: list[str] = []
    target = name.encode("latin-1")
    for raw in _header_values(scope, b"cookie"):
        for part in raw.split(b";"):
            key, sep, value = part.strip().partition(b"=")
            if sep and key == target:
                found.append(value.decode("latin-1"))
    if len(found) != 1 or not found[0]:
        return None
    return found[0]


# ── 用戶端 IP ────────────────────────────────────────────────────────


def parse_trusted_proxies(raw: str) -> tuple[IpNetwork, ...]:
    """逗號分隔的 IP／CIDR；格式錯直接拒絕啟動（不默默忽略打錯的項目）。"""
    networks: list[IpNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ConfigError(
                f"ui.trusted_proxies 有無法解析的項目：{item!r}"
            ) from None
    return tuple(networks)


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def client_ip(scope: Scope, trusted: Iterable[IpNetwork]) -> str:
    """限流用的來源 key。

    直接連線來源在受信任代理清單內、且恰好一個可解析的 `CF-Connecting-IP` 時才採信它；
    其餘情況一律用直接連線來源（非 IP 字串如 TestClient 的 "testclient" 當不透明 key，
    永不視為受信任代理）。不看 X-Forwarded-For。
    """
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    peer_ip = _as_ip(peer)
    if peer_ip is None or not any(peer_ip in net for net in trusted):
        return peer
    values = _header_values(scope, CF_CONNECTING_IP)
    if len(values) != 1:
        return peer
    forwarded = _as_ip(values[0].decode("latin-1").strip())
    return str(forwarded) if forwarded is not None else peer


# ── session ─────────────────────────────────────────────────────────


@dataclass
class _Session:
    created: float
    last_seen: float


@dataclass(frozen=True)
class SessionInfo:
    created: float
    absolute_expires: float
    idle_expires: float


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class SessionStore:
    """記憶體內 session；執行緒安全（同步路由在 threadpool 執行）。"""

    def __init__(
        self,
        *,
        absolute_seconds: float,
        idle_seconds: float,
        max_sessions: int,
        clock: Clock,
    ) -> None:
        self._absolute = absolute_seconds
        self._idle = idle_seconds
        self._max = max_sessions
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    @property
    def absolute_seconds(self) -> float:
        return self._absolute

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._sessions) >= self._max:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].created)
                del self._sessions[oldest]
            self._sessions[_digest(session_id)] = _Session(created=now, last_seen=now)
        return session_id

    def touch(self, session_id: str) -> SessionInfo | None:
        """有效就更新最後使用時間並回傳期限；過期（順手刪除）或不存在回 None。"""
        key = _digest(session_id)
        now = self._clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if self._expired(session, now):
                del self._sessions[key]
                return None
            session.last_seen = now
            return self._info(session)

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(_digest(session_id), None) is not None

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._sessions)

    def _expired(self, session: _Session, now: float) -> bool:
        return (
            now >= session.created + self._absolute
            or now >= session.last_seen + self._idle
        )

    def _info(self, session: _Session) -> SessionInfo:
        absolute = session.created + self._absolute
        return SessionInfo(
            created=session.created,
            absolute_expires=absolute,
            idle_expires=min(session.last_seen + self._idle, absolute),
        )

    def _prune(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[key]


# ── 登入限流 ────────────────────────────────────────────────────────


class LoginLimiter:
    """時間窗內的失敗次數上限 + 指數退避，分「每個來源」與「全域」兩層。"""

    GLOBAL = "\x00global"

    def __init__(
        self,
        *,
        window: float,
        max_per_ip: int,
        max_global: int,
        lockout: float,
        clock: Clock,
    ) -> None:
        self._window = window
        self._limits = {"ip": max_per_ip, "global": max_global}
        self._lockout = lockout
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, ip: str) -> float:
        """目前是否在退避中；回傳還要等幾秒（0 = 可以嘗試）。"""
        now = self._clock()
        with self._lock:
            return max(
                self._wait(ip, self._limits["ip"], now),
                self._wait(self.GLOBAL, self._limits["global"], now),
            )

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            for key in (ip, self.GLOBAL):
                self._failures.setdefault(key, deque()).append(now)
                self._prune(key, now)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures.get(key)
        if entries is None:
            return deque()
        while entries and entries[0] <= now - self._window:
            entries.popleft()
        if not entries:
            del self._failures[key]
        return entries

    def _wait(self, key: str, limit: int, now: float) -> float:
        entries = self._prune(key, now)
        count = len(entries)
        if count < limit:
            return 0.0
        # 剛達上限鎖 base，之後每多一次失敗加倍；不超過時間窗
        lock = min(self._lockout * 2 ** (count - limit), self._window)
        return max(0.0, entries[-1] + lock - now)


# ── 組裝 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UiAuth:
    """app 內共用的 UI 認證元件。"""

    sessions: SessionStore
    limiter: LoginLimiter
    trusted_proxies: tuple[IpNetwork, ...]
    cookie_secure: bool

    @property
    def cookie_name(self) -> str:
        return cookie_name(self.cookie_secure)

    @classmethod
    def from_config(cls, config: UiConfig, clock: Clock) -> UiAuth:
        return cls(
            sessions=SessionStore(
                absolute_seconds=config.session_absolute_hours * 3600,
                idle_seconds=config.session_idle_minutes * 60,
                max_sessions=config.max_sessions,
                clock=clock,
            ),
            limiter=LoginLimiter(
                window=config.login_failure_window_seconds,
                max_per_ip=config.login_max_failures_per_ip,
                max_global=config.login_max_failures_global,
                lockout=config.login_lockout_seconds,
                clock=clock,
            ),
            trusted_proxies=parse_trusted_proxies(config.trusted_proxies),
            cookie_secure=config.cookie_secure,
        )
