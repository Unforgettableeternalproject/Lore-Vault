"""核心層：Vault／Note／Episode／Concept／Injection 型別（純標準庫）。"""

from ._base import MISSING, SchemaError
from .chars import InvalidCharacters
from .models import (
    CONCEPT_KINDS,
    EPISODE_ORIGINS,
    SPACE_DEV,
    SPACES,
    VAULT_KINDS,
    Concept,
    Episode,
    Injection,
    Note,
    SourceTurn,
    ToolCount,
    Vault,
    canonical_key,
)

__all__ = [
    "CONCEPT_KINDS",
    "EPISODE_ORIGINS",
    "MISSING",
    "SPACES",
    "SPACE_DEV",
    "VAULT_KINDS",
    "Concept",
    "Episode",
    "Injection",
    "InvalidCharacters",
    "Note",
    "SchemaError",
    "SourceTurn",
    "ToolCount",
    "Vault",
    "canonical_key",
]
