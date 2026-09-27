"""UI 的本地身分驗證（A21／A23）：服務端 session、登入序列化、用戶端 IP 判定。

- 登入用 DB 內的 UI 帳號密碼（A23，規則與鎖定見 `storage.ui_login`）；
  session 的 principal = username，另記顯示名稱（前端署名用）
- session 只存在服務程序記憶體：重啟即全部失效（使用者重新登入即可），
  不寫資料庫、不留可被帶走的憑證。以 sha256(session id) 當鍵，記憶體裡沒有原值
- 期限：絕對期限（登入後固定時長）與閒置期限（最後一次認證請求起算），先到先失效
- 登入嘗試在本程序內序列化（`login_lock`）：全域失敗計數才不會被並行請求搶過
- CSRF：cookie 路徑一律要求 `X-Lore-Vault-UI: 1` 標頭（跨來源請求帶自訂標頭必須
  先過 CORS preflight，本服務不回 CORS 標頭，所以瀏覽器送不出來），外加 SameSite=Strict
- `client_ip` 判定的來源只用於登入紀錄與 log（A23 起不再依 IP 限流）
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

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
    """登入紀錄用的來源 IP。

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
    # 登入帳號（= principal，A22）與顯示名稱（前端署名，A23）
    principal: str
    display_name: str


@dataclass(frozen=True)
class SessionInfo:
    created: float
    absolute_expires: float
    idle_expires: float
    principal: str
    display_name: str


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

    def create(self, principal: str, display_name: str) -> str:
        if not principal or not display_name:
            raise ValueError("session 必須記錄 principal 與顯示名稱")
        session_id = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._sessions) >= self._max:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].created)
                del self._sessions[oldest]
            self._sessions[_digest(session_id)] = _Session(
                created=now,
                last_seen=now,
                principal=principal,
                display_name=display_name,
            )
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
            principal=session.principal,
            display_name=session.display_name,
        )

    def _prune(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[key]


# ── 組裝 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UiAuth:
    """app 內共用的 UI 認證元件。"""

    sessions: SessionStore
    trusted_proxies: tuple[IpNetwork, ...]
    cookie_secure: bool
    clock: Clock
    # 同一時間只跑一個登入嘗試（全域失敗計數的嚴格順序）
    login_lock: threading.Lock = field(default_factory=threading.Lock)

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
            trusted_proxies=parse_trusted_proxies(config.trusted_proxies),
            cookie_secure=config.cookie_secure,
            clock=clock,
        )
