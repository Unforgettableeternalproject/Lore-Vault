"""問答模型用戶端（D11）：OpenAI chat completions + 結構化輸出（json_schema strict）。

沿用摘要用戶端（`enrich.clients`）的傳輸層、狀態碼分類與 key 遮蔽：
明確帶 `reasoning_effort` 與 `max_completion_tokens`（D4：不帶 effort 會回空字串且
不報錯）；`finish_reason` 不是 `stop`、內容為空都視為失敗，不回假成功。
JSON 解析與結構檢查在服務層（`ask.service`），這裡只回原始文字與 usage。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from lore_vault.config import AskConfig, Secret
from lore_vault.enrich.clients import (
    EnrichError,
    InvalidOutput,
    ProviderUnavailable,
    Transport,
    _check_status,
    _parse_json,
    urllib_transport,
)

STATUS_ANSWERED = "answered"
STATUS_INSUFFICIENT = "insufficient"
STATUSES = (STATUS_ANSWERED, STATUS_INSUFFICIENT)

# 與 D11 實測相同的 schema
RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "ask_answer",
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(STATUSES)},
                "points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "note_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["claim", "note_ids"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["status", "points"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# usage 只轉出這幾個數字（其他欄位各家不同、對呼叫端沒用）
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class Answerer(Protocol):
    model: str

    def complete(self, system: str, user: str) -> Completion: ...


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    usage = {k: raw[k] for k in _USAGE_KEYS if isinstance(raw.get(k), int)}
    details = raw.get("completion_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        usage["reasoning_tokens"] = details["reasoning_tokens"]
    return usage


class OpenAIAnswerer:
    """`ask` 設定（`[ask]`）的 OpenAI 用戶端；repr 與錯誤訊息不含 key。"""

    def __init__(
        self,
        config: AskConfig,
        api_key: Secret | None,
        *,
        transport: Transport = urllib_transport,
    ) -> None:
        if not isinstance(api_key, Secret) or not api_key.reveal().strip():
            raise ProviderUnavailable("缺少 OpenAI API key（環境變數 OPENAI_API_KEY）")
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.reasoning_effort = config.reasoning_effort
        self.max_completion_tokens = config.max_completion_tokens
        self.timeout = config.timeout
        self._api_key = api_key
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"OpenAIAnswerer(model={self.model!r}, "
            f"reasoning_effort={self.reasoning_effort!r})"
        )

    def _scrub(self, text: str) -> str:
        key = self._api_key.reveal()
        return text.replace(key, "***") if key else text

    def request_payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_completion_tokens,
            "response_format": RESPONSE_FORMAT,
        }

    def complete(self, system: str, user: str) -> Completion:
        payload = json.dumps(
            self.request_payload(system, user), ensure_ascii=False
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key.reveal()}",
        }
        try:
            resp = self._transport(
                f"{self.base_url}/chat/completions", payload, headers, self.timeout
            )
        except EnrichError as exc:
            raise type(exc)(self._scrub(str(exc))) from None
        _check_status("OpenAI", resp, self._scrub)
        data = _parse_json("OpenAI", resp)
        try:
            choice = data["choices"][0]
            finish = choice.get("finish_reason")
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise InvalidOutput("OpenAI 回應缺少 choices／message") from None
        if finish == "length":
            raise InvalidOutput(
                f"回答被截斷（finish_reason=length，max_completion_tokens="
                f"{self.max_completion_tokens}）"
            )
        if finish != "stop":
            raise InvalidOutput(f"回答未正常結束（finish_reason={finish!r}）")
        if not isinstance(content, str) or not content.strip():
            raise InvalidOutput("回答為空字串")
        model = data.get("model") if isinstance(data.get("model"), str) else self.model
        return Completion(content, model, _usage(data.get("usage")))
