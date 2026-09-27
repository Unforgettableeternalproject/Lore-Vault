"""hook／主機管線用的最小 HTTP 客戶端（`urllib`，只用標準庫）。

錯誤分兩類（與 MCP 殼 `lore_vault.mcp.client` 同一套分法）：
- `ServiceUnavailable`：連線失敗、逾時、502／503／504、Cloudflare 521–524／530
- `ServiceRejected`：3xx（Access 導向登入）、401／403、其他 4xx、其餘 5xx、非 JSON 回應

兩者對 spool 的處理相同（留在本地、下次再推），分開是為了讓訊息指出要查哪裡。
不跟隨重導向；例外訊息只含狀態碼與錯誤類型，不含 header 或 token。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .client_env import ClientSettings

UNREACHABLE_STATUSES = frozenset({502, 503, 504, 521, 522, 523, 524, 530})
_MAX_DETAIL = 200
# 4xx 回應 body 的讀取上限（逐筆結果用；超過就不解析，只留狀態碼）
_MAX_ERROR_BODY = 4 * 1024 * 1024


class ServiceError(Exception):
    """推送／拉取失敗的共同基底。`detail` 不含密鑰。

    `body`：服務回的 JSON 錯誤內容（例如 `POST /v1/concepts` 整批拒收時的逐筆結果）；
    讀不到、不是 JSON 或太大時為 None。
    """

    def __init__(
        self, detail: str, status: int | None = None, body: Any = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail[:_MAX_DETAIL]
        self.status = status
        self.body = body


def _error_payload(exc: urllib.error.HTTPError) -> Any:
    """盡力讀出 4xx 的 JSON body；任何失敗都回 None（錯誤路徑不再拋第二個例外）。"""
    try:
        raw = exc.read(_MAX_ERROR_BODY + 1)
    except (OSError, ValueError, AttributeError):
        return None
    if not raw or len(raw) > _MAX_ERROR_BODY:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


class ServiceUnavailable(ServiceError):
    """服務不可達（稍後重試即可）。"""


class ServiceRejected(ServiceError):
    """服務或中間層明確拒絕（多半是設定問題）。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def auth_headers(settings: ClientSettings) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.token is not None:
        headers["Authorization"] = f"Bearer {settings.token.reveal()}"
    if settings.cf_access is not None:
        client_id, secret = settings.cf_access
        headers["CF-Access-Client-Id"] = client_id.reveal()
        headers["CF-Access-Client-Secret"] = secret.reveal()
    return headers


def error_code(body: Any) -> str | None:
    """服務的錯誤格式 `{"error": {"code": ...}}` 取出 code；不是這個形狀回 None
    （例如 Cloudflare Access 的 403 頁面）。"""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


def _rejected_detail(status: int, payload: Any = None) -> str:
    code = error_code(payload)
    if code is not None:
        # 服務自己的拒絕（例如 403 episode_ingest_disabled），不是 Access 問題
        message = payload["error"].get("message")
        return f"HTTP {status} {code}" + (f"：{message}" if message else "")
    if 300 <= status < 400:
        return (
            f"HTTP {status}：被導向"
            "（通常是 Cloudflare Access 要求登入，檢查 CF_ACCESS_*）"
        )
    if status == 401:
        return "HTTP 401：LORE_VAULT_API_TOKEN 與服務端不一致或未帶上"
    if status == 403:
        return "HTTP 403：存取被拒（檢查 CF_ACCESS_CLIENT_ID／SECRET）"
    return f"HTTP {status}"


def request_json(
    settings: ClientSettings,
    method: str,
    path: str,
    body: Any = None,
    *,
    timeout: float,
    query: dict[str, str] | None = None,
) -> Any:
    """送一個 JSON 請求，2xx 回傳解析後的 JSON；其他情況拋 `ServiceError` 子類。"""
    if not settings.url:
        raise ServiceRejected("未設定服務位址")
    url = settings.url + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json", **auth_headers(settings)}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = _error_payload(exc) if 400 <= status < 500 else None
        exc.close()
        if status in UNREACHABLE_STATUSES:
            raise ServiceUnavailable(f"HTTP {status}", status) from None
        raise ServiceRejected(
            _rejected_detail(status, payload), status, payload
        ) from None
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
    ) as exc:
        reason = getattr(exc, "reason", exc)
        raise ServiceUnavailable(f"連線失敗（{type(reason).__name__}）") from None
    except OSError as exc:
        raise ServiceUnavailable(f"連線失敗（{type(exc).__name__}）") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ServiceRejected(
            f"HTTP {status}：回應不是 JSON（可能被中間層攔截）", status
        ) from None
