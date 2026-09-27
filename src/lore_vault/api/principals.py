"""憑證 → principal 對照（A22）。

principal 是服務依「請求用了哪一組憑證」判定的主體，寫進 note 的 `principal`／
`updated_by_principal`；**不可由請求指定**（請求 body 帶 `principal` 會被
`extra="forbid"` 以 422 拒絕）。

- 現階段只有一組憑證（`LORE_VAULT_API_TOKEN`），對應 `LORE_VAULT_PRINCIPAL`
  （D12；未設時為 `DEFAULT_PRINCIPAL` = `owner`，由 `ApiSettings.principal` 帶入）
- 設計成對照表：日後共享時一 token 一 principal，只要多加項目
- 只用於 Bearer 路徑；UI 登入（A23）的 principal 是 DB 內 UI 帳號的 username
  （首次啟動建立的管理員預設同 principal）
- 認證中介層把判定結果放進 ASGI scope 的 `state`，路由以 `principal_of` 取用；
  缺少時拋錯（fail closed），不預設成任何人
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence

from starlette.requests import Request
from starlette.types import Scope

from lore_vault.config import Secret
from lore_vault.schema import DEFAULT_PRINCIPAL

SCOPE_KEY = "lore_principal"


class Principals:
    """(憑證, principal) 的對照表。比對一律走過每一項（常數時間、不提早結束）。"""

    def __init__(self, entries: Sequence[tuple[Secret, str]]) -> None:
        if not entries:
            raise ValueError("至少要有一組憑證")
        seen: set[bytes] = set()
        table: list[tuple[bytes, str]] = []
        for secret, principal in entries:
            raw = secret.reveal().encode("utf-8")
            if raw in seen:
                raise ValueError("同一組憑證不可對應多個 principal")
            if not principal or principal != principal.strip():
                raise ValueError(f"principal 名稱不合法：{principal!r}")
            seen.add(raw)
            table.append((raw, principal))
        self._table = tuple(table)

    @classmethod
    def single(cls, token: Secret, principal: str = DEFAULT_PRINCIPAL) -> Principals:
        return cls([(token, principal)])

    def match(self, provided: bytes) -> str | None:
        found: str | None = None
        for expected, principal in self._table:
            if hmac.compare_digest(provided, expected) and found is None:
                found = principal
        return found


def set_principal(scope: Scope, principal: str) -> None:
    state = scope.setdefault("state", {})
    state[SCOPE_KEY] = principal


def principal_of(request: Request) -> str:
    """認證中介層判定的 principal。缺少代表請求沒經過認證路徑，是程式錯誤（500）。"""
    principal = request.scope.get("state", {}).get(SCOPE_KEY)
    if not isinstance(principal, str) or not principal:
        raise RuntimeError("請求缺少 principal（未經認證中介層）")
    return principal
