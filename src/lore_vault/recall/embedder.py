"""查詢端的 embedding 介面。本層不直接連 Ollama：由呼叫端注入 `Embedder`。

與 `lore_vault.enrich.clients.Embedder` 結構相容（同樣是 `embed(text) -> 向量`），
Ollama 用戶端可直接傳進來。逾時由 embedder 自己負責（例如 HTTP timeout），
逾時請拋 `TimeoutError`；本層不另開執行緒計時。

任何失敗（沒有 embedder、拋例外、逾時、回空向量或維度不符）都不往上拋，
而是回傳 `QueryVector(vector=None, reason=...)`，由呼叫端降級並標示。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from lore_vault.storage.vectors import normalize

# 降級原因代碼（回傳給 HTTP／MCP 層的穩定字串）
REASON_UNAVAILABLE = "embedder_unavailable"
REASON_TIMEOUT = "embedder_timeout"
REASON_ERROR = "embedder_error"
REASON_INVALID = "embedder_invalid_vector"

# 例外訊息最多保留幾個字，避免把整段 HTTP 回應塞進回傳
_MAX_DETAIL = 200


class Embedder(Protocol):
    def embed(self, text: str) -> Sequence[float]: ...


@dataclass(frozen=True)
class QueryVector:
    """`vector` 為 None 時 `reason`／`detail` 說明原因。"""

    vector: np.ndarray | None
    reason: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.vector is not None


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


def embed_text(embedder: Embedder | None, text: str, *, dim: int) -> QueryVector:
    """算出正規化後的向量；失敗時回傳原因，不拋例外。"""
    if embedder is None:
        return QueryVector(None, REASON_UNAVAILABLE, "未設定 embedder")
    try:
        raw = embedder.embed(text)
    except TimeoutError as exc:
        return QueryVector(None, REASON_TIMEOUT, _describe(exc))
    except Exception as exc:  # noqa: BLE001 - 任何 embedder 失敗都降級
        return QueryVector(None, REASON_ERROR, _describe(exc))
    try:
        # 空向量、None、維度不符、NaN、零向量都在這裡擋下
        vector = normalize(raw, dim)
    except Exception as exc:  # noqa: BLE001
        return QueryVector(None, REASON_INVALID, _describe(exc))
    return QueryVector(vector)
