"""殼端 HTTP 客戶端：帶 bearer token（與選用的 Cloudflare Access header）轉發到服務。

錯誤分兩類，決定降級與否：
- `ServiceUnreachable`：連線失敗、逾時、協定錯誤、502／503／504
  與 Cloudflare 521–524／530
  → 讀取類工具可改讀本地快照
- `ServiceError`：3xx（Cloudflare Access 導向登入）、401／403、其他 4xx、500 等其餘
  5xx、非 JSON 回應 → 直接回報，**不可**降級（降級會把設定錯誤或服務端資料問題藏起來）

密鑰只放在請求 header；例外訊息、log 都不含 header 或 token。
不跟隨重導向：Access 缺 token 時的 302 必須被當成錯誤看見。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx2

from .settings import ShellSettings

# 例外細節最多保留幾個字
_MAX_DETAIL = 300

# 視為「服務不可達」的狀態碼：閘道／代理回報上游不可用。
# 521–524、530 是 Cloudflare 回報 origin 不可達（tunnel 斷線常見 530）。
# 500（含服務自己的 storage_error）是服務端明確的錯誤，不可被降級掩蓋。
UNREACHABLE_STATUSES = frozenset({502, 503, 504, 521, 522, 523, 524, 530})


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


class ServiceUnreachable(Exception):
    """服務不可達（連線、逾時、5xx）。`detail` 為給 agent 看的簡述，不含密鑰。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ServiceError(Exception):
    """服務（或中間層）明確拒絕：狀態碼 + 服務的錯誤 body（若有）。"""

    def __init__(
        self, status: int, message: str, body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body

    @property
    def code(self) -> str | None:
        if self.body and isinstance(self.body.get("error"), dict):
            code = self.body["error"].get("code")
            return code if isinstance(code, str) else None
        return None


def _json_or_none(response: httpx2.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _status_error(response: httpx2.Response) -> ServiceError:
    status = response.status_code
    body = _json_or_none(response)
    if 300 <= status < 400:
        return ServiceError(
            status,
            f"服務回應重導向（HTTP {status}），通常是 Cloudflare Access 要求登入："
            "確認 CF_ACCESS_CLIENT_ID／CF_ACCESS_CLIENT_SECRET"
            "（或 mcp.cf_access_env_file）已設定且有效，並確認 mcp.base_url 正確",
        )
    if status == 401:
        return ServiceError(
            status,
            "認證失敗（HTTP 401）：LORE_VAULT_API_TOKEN 與服務端不一致或未帶上",
            body,
        )
    if status == 403:
        return ServiceError(
            status,
            "存取被拒（HTTP 403）：通常是 Cloudflare Access 拒絕，確認 "
            "CF_ACCESS_CLIENT_ID／CF_ACCESS_CLIENT_SECRET 是否有效",
            body,
        )
    if body is not None and isinstance(body.get("error"), dict):
        message = str(body["error"].get("message") or f"HTTP {status}")
        return ServiceError(status, message, body)
    return ServiceError(
        status, f"HTTP {status}：服務回應不是預期的錯誤格式（可能被中間層攔截）"
    )


def _unreachable_from_status(response: httpx2.Response) -> ServiceUnreachable:
    body = _json_or_none(response)
    detail = f"HTTP {response.status_code}"
    if body is not None and isinstance(body.get("error"), dict):
        error = body["error"]
        detail += f" {error.get('code')}: {error.get('message')}"
    return ServiceUnreachable(_clip(detail))


def _unreachable_from_exc(exc: httpx2.TransportError) -> ServiceUnreachable:
    return ServiceUnreachable(_clip(f"{type(exc).__name__}: {exc}"))


class ServiceClient:
    def __init__(
        self,
        settings: ShellSettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {settings.token.reveal()}"}
        if settings.cf_access is not None:
            client_id, secret = settings.cf_access
            headers["CF-Access-Client-Id"] = client_id.reveal()
            headers["CF-Access-Client-Secret"] = secret.reveal()
        self.base_url = settings.base_url
        self._client = httpx2.AsyncClient(
            base_url=settings.base_url,
            headers=headers,
            timeout=settings.timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"ServiceClient({self.base_url!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST JSON；2xx 回 dict，否則拋 `ServiceUnreachable` 或 `ServiceError`。"""
        try:
            response = await self._client.post(path, json=body)
        except httpx2.TransportError as exc:
            raise _unreachable_from_exc(exc) from None
        if response.status_code in UNREACHABLE_STATUSES:
            raise _unreachable_from_status(response)
        if not 200 <= response.status_code < 300:
            raise _status_error(response)
        data = _json_or_none(response)
        if data is None:
            raise ServiceError(
                response.status_code,
                "服務回應不是 JSON 物件（可能被中間層攔截，確認 mcp.base_url）",
            )
        return data

    async def download_snapshot(
        self, dest: Path, *, if_none_match: str | None = None
    ) -> tuple[int, dict[str, str]]:
        """串流 `GET /v1/snapshot` 到暫存檔 `dest`；回傳（狀態碼, header）。

        帶 `if_none_match`（本地快照 sha256）且服務端未變時回 304、不寫檔。
        中途失敗時 `dest` 可能是半檔，由呼叫端刪除。
        """
        headers_out = {}
        if if_none_match:
            headers_out["If-None-Match"] = f'"{if_none_match}"'
        try:
            async with self._client.stream(
                "GET", "/v1/snapshot", headers=headers_out
            ) as response:
                if response.status_code in UNREACHABLE_STATUSES:
                    await response.aread()
                    raise _unreachable_from_status(response)
                if response.status_code == 304:
                    await response.aread()
                    return 304, {k.lower(): v for k, v in response.headers.items()}
                if response.status_code != 200:
                    await response.aread()
                    raise _status_error(response)
                with open(dest, "wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                headers = {k.lower(): v for k, v in response.headers.items()}
        except httpx2.TransportError as exc:
            raise _unreachable_from_exc(exc) from None
        return 200, headers
