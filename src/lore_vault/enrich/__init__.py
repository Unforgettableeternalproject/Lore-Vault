"""背景補算：summary（OpenAI）與 embedding（Ollama）。見 D4／A14。

hook 路徑不得 import 本套件（會帶進 numpy 與網路用戶端）。
"""

from .clients import (
    SUMMARY_SYSTEM_PROMPT,
    Embedder,
    EnrichError,
    EnrichTimeout,
    HttpResponse,
    InvalidOutput,
    OllamaEmbedder,
    OpenAISummarizer,
    ProviderUnavailable,
    RateLimited,
    Summarizer,
    Transport,
)
from .worker import EnrichWorker, RateLimiter, RunStats, embedding_text

__all__ = [
    "SUMMARY_SYSTEM_PROMPT",
    "Embedder",
    "EnrichError",
    "EnrichTimeout",
    "EnrichWorker",
    "HttpResponse",
    "InvalidOutput",
    "OllamaEmbedder",
    "OpenAISummarizer",
    "ProviderUnavailable",
    "RateLimited",
    "RateLimiter",
    "RunStats",
    "Summarizer",
    "Transport",
    "embedding_text",
]
