"""ask()（D11）：檢索片段交問答模型整理成逐點回答，附引用防呆。"""

from .client import Answerer, Completion, OpenAIAnswerer
from .service import (
    AskContext,
    AskError,
    AskInvalidOutput,
    AskNotConfigured,
    AskProviderError,
    AskRateLimited,
    AskResult,
    AskTimeout,
    ask,
    generate,
    prepare,
)

__all__ = [
    "Answerer",
    "AskContext",
    "AskError",
    "AskInvalidOutput",
    "AskNotConfigured",
    "AskProviderError",
    "AskRateLimited",
    "AskResult",
    "AskTimeout",
    "Completion",
    "OpenAIAnswerer",
    "ask",
    "generate",
    "prepare",
]
