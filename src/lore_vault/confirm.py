"""破壞性操作的兩段式確認 token（只用標準庫）。

服務端（`api.manage`）與 stdio MCP 殼（`tasks.mcp_tools` 的 archive）共用同一份簽章
邏輯。刻意放在不依賴 fastapi 的中性模組：殼若直接 import `api.manage`，啟動就要多載
FastAPI 與整個 API 層（約 0.6 秒），而延遲到用時才載入又會讓版本錯位拖到 archive
當下才爆。格式與語意見 `api/manage.py` 模組說明。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CONFIRM_TTL_SECONDS = 300


class ConfirmTokenInvalid(ValueError):
    """確認 token 無法解析、簽章不符，或與這次請求的操作／參數不符（竄改或誤用）。"""


class ConfirmTokenExpired(ValueError):
    """確認 token 已過期：重新規劃取得新 token。"""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def plan_digest(fingerprint: Any) -> str:
    return hashlib.sha256(canonical(fingerprint)).hexdigest()


@dataclass
class ConfirmSigner:
    """簽發與驗證確認 token。祕密與時鐘掛在 app.state 上（測試可注入時鐘）。"""

    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    ttl: float = CONFIRM_TTL_SECONDS
    clock: Callable[[], float] = time.time

    def _sign(self, body: bytes) -> str:
        return b64(hmac.new(self.secret, body, hashlib.sha256).digest())

    def issue(self, op: str, args: dict[str, Any], digest: str) -> tuple[str, float]:
        expires = self.clock() + self.ttl
        payload = {"op": op, "args": args, "digest": digest, "exp": expires}
        body = canonical(payload)
        return f"{b64(body)}.{self._sign(body)}", expires

    def verify(self, token: str, op: str, args: dict[str, Any]) -> str:
        """回傳 token 綁定的規劃 digest。簽章在前：竄改過的內容一律不解讀。"""
        try:
            encoded, signature = token.split(".")
            body = unb64(encoded)
        except (ValueError, binascii.Error):
            raise ConfirmTokenInvalid("確認 token 格式錯誤") from None
        if not hmac.compare_digest(signature, self._sign(body)):
            raise ConfirmTokenInvalid("確認 token 簽章不符（遭竄改或來自其他服務程序）")
        payload = json.loads(body)
        if payload.get("op") != op or payload.get("args") != args:
            raise ConfirmTokenInvalid(
                "確認 token 與這次請求的操作或參數不符；請以規劃時相同的參數送出"
            )
        if self.clock() > float(payload["exp"]):
            raise ConfirmTokenExpired("確認 token 已過期，請重新規劃")
        return str(payload["digest"])
