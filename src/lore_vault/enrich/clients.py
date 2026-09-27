"""Embedding（Ollama `/api/embed`）與摘要（OpenAI chat completions）的 HTTP 用戶端。

HTTP 走標準庫 `urllib`，傳輸層可注入（測試用 fake，不打網路）：
`Transport = (url, body, headers, timeout) -> HttpResponse`。

錯誤分三類，worker 依此決定怎麼記：
- `ProviderUnavailable`：服務連不上、認證或模型設定錯（401／403／404）。
  不是這則 note 的問題，本輪該種補算中止、不消耗嘗試次數。
- `RateLimited`（429）：消耗一次嘗試，本輪該種補算停止，帶 `retry_after`。
- 其他 `EnrichError`（逾時、5xx、空輸出、截斷、維度不符）：消耗一次嘗試。

錯誤訊息只含狀態碼與截斷後的回應內容，不含 headers；摘要用戶端另外把 key
從訊息中遮掉，repr 也不含 key。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from lore_vault.config import EmbeddingConfig, Secret, SummaryConfig

# 錯誤訊息中回應內容最多帶幾個字
_BODY_EXCERPT = 200

# 摘要 prompt（D4）：繁體中文 1–2 句、只陳述結論與關鍵數據、不加評價
SUMMARY_SYSTEM_PROMPT = (
    "你是筆記摘要器。請用繁體中文寫 1 到 2 句摘要，盡量精簡。"
    "只陳述筆記的結論與關鍵數據（數字、名稱、版本、決定），"
    "不加評價或形容詞，不加入原文沒有的資訊，不要前言、標題或條列，直接輸出摘要本身。"
)


class EnrichError(Exception):
    """補算失敗（消耗一次嘗試）。"""


class ProviderUnavailable(EnrichError):
    """服務不可用或設定錯誤：本輪中止，不消耗嘗試次數。"""


class RateLimited(EnrichError):
    """429：消耗一次嘗試，本輪停止同種補算。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EnrichTimeout(EnrichError):
    """請求逾時。"""


class InvalidOutput(EnrichError):
    """回應格式錯、空字串、被截斷、維度不符等。"""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


# body 為 None 時是 GET（例如 Ollama `/api/ps`），否則 POST JSON
Transport = Callable[[str, bytes | None, Mapping[str, str], float], HttpResponse]


def urllib_transport(
    url: str, body: bytes | None, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    """預設傳輸：有 body 為 POST JSON、None 為 GET。

    HTTP 錯誤狀態照樣回傳，由用戶端判斷。
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        with exc:
            payload = exc.read()
        return HttpResponse(exc.code, payload, dict(exc.headers.items()))
    except TimeoutError as exc:
        raise EnrichTimeout(f"請求逾時（{timeout} 秒）：{url}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise EnrichTimeout(f"請求逾時（{timeout} 秒）：{url}") from exc
        raise ProviderUnavailable(f"無法連線 {url}：{exc.reason}") from exc


def _excerpt(body: bytes) -> str:
    return body.decode("utf-8", errors="replace").strip()[:_BODY_EXCERPT]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


def _check_status(
    service: str, resp: HttpResponse, scrub: Callable[[str], str]
) -> None:
    if 200 <= resp.status < 300:
        return
    detail = scrub(_excerpt(resp.body))
    message = f"{service} 回應 HTTP {resp.status}：{detail}"
    if resp.status == 429:
        raise RateLimited(message, _retry_after(resp.headers))
    if resp.status in (401, 403, 404):
        raise ProviderUnavailable(message)
    raise EnrichError(message)


def _parse_json(service: str, resp: HttpResponse) -> Any:
    try:
        return json.loads(resp.body)
    except ValueError:
        raise InvalidOutput(f"{service} 回應不是 JSON：{_excerpt(resp.body)}") from None


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, text: str) -> Sequence[float]: ...


class Summarizer(Protocol):
    model: str

    def summarize(self, title: str, body: str) -> str: ...


def _identity(text: str) -> str:
    return text


def _keep_alive_value(raw: str) -> str | int | None:
    """設定值 → Ollama `keep_alive`：空白＝不送；純整數（含負數）送秒數，
    其餘原樣送 duration 字串。"""
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


class OllamaEmbedder:
    """Ollama `/api/embed`；回傳向量先驗維度。"""

    def __init__(
        self, config: EmbeddingConfig, *, transport: Transport = urllib_transport
    ) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.dim = config.dim
        self.timeout = config.timeout
        self.keep_alive = _keep_alive_value(config.keep_alive)
        self._transport = transport

    def __repr__(self) -> str:
        return f"OllamaEmbedder(base_url={self.base_url!r}, model={self.model!r})"

    def embed(self, text: str) -> list[float]:
        body: dict[str, Any] = {"model": self.model, "input": text}
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        payload = json.dumps(body).encode()
        resp = self._transport(
            f"{self.base_url}/api/embed",
            payload,
            {"Content-Type": "application/json"},
            self.timeout,
        )
        _check_status("Ollama", resp, _identity)
        data = _parse_json("Ollama", resp)
        try:
            vector = data["embeddings"][0]
        except (KeyError, IndexError, TypeError):
            raise InvalidOutput("Ollama 回應缺少 embeddings") from None
        if not isinstance(vector, list) or len(vector) != self.dim:
            size = len(vector) if isinstance(vector, list) else type(vector).__name__
            raise InvalidOutput(f"embedding 維度 {size} 與設定 {self.dim} 不符")
        return vector


def ollama_model_loaded(
    base_url: str, model: str, *, transport: Transport, timeout: float
) -> bool | None:
    """Ollama `/api/ps`：模型目前是否載入在記憶體。

    回 True／False；查不到（連不上、逾時、非 200、格式不對）回 None，由呼叫端決定退路。
    模型名比對容許省略 tag（設定 `bge-m3` 對上 `bge-m3:latest`）。
    """
    try:
        resp = transport(f"{base_url.rstrip('/')}/api/ps", None, {}, timeout)
        if resp.status != 200:
            return None
        models = json.loads(resp.body).get("models")
    except Exception:  # noqa: BLE001 - 探測失敗一律視為未知
        return None
    if not isinstance(models, list):
        return None
    wanted = {model, f"{model}:latest"} if ":" not in model else {model}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "model"):
            if entry.get(key) in wanted:
                return True
    return False


class OpenAISummarizer:
    """OpenAI chat completions；明確帶 `reasoning_effort` 與 `max_completion_tokens`。

    空字串、只有空白、`finish_reason` 不是 `stop`（含 `length` 截斷）都視為失敗
    （D4：不指定 effort 時推理吃光 token、回空字串且不報錯）。
    """

    def __init__(
        self,
        config: SummaryConfig,
        api_key: Secret,
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
            f"OpenAISummarizer(model={self.model!r}, "
            f"reasoning_effort={self.reasoning_effort!r})"
        )

    def _scrub(self, text: str) -> str:
        key = self._api_key.reveal()
        return text.replace(key, "***") if key else text

    def request_payload(self, title: str, body: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": f"標題：{title}\n\n{body}"},
            ],
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_completion_tokens,
        }

    def summarize(self, title: str, body: str) -> str:
        payload = json.dumps(
            self.request_payload(title, body), ensure_ascii=False
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
            # 傳輸層訊息理論上不含 key，保險起見仍遮一次
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
                f"摘要被截斷（finish_reason=length，max_completion_tokens="
                f"{self.max_completion_tokens}）"
            )
        if finish != "stop":
            raise InvalidOutput(f"摘要未正常結束（finish_reason={finish!r}）")
        if not isinstance(content, str) or not content.strip():
            raise InvalidOutput("摘要為空字串")
        return content.strip()
