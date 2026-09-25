"""核心層：git remote → 穩定 vault key。"""

from .aliases import AliasConflictError, VaultIndex, lookup_key, resolve_vault
from .remote import (
    SOURCE_FOLDER,
    SOURCE_GIT_REMOTE,
    Binding,
    display_from_remote,
    folder_key,
    git_remote,
    normalize_remote,
    resolve_binding,
)

__all__ = [
    "SOURCE_FOLDER",
    "SOURCE_GIT_REMOTE",
    "AliasConflictError",
    "Binding",
    "VaultIndex",
    "display_from_remote",
    "folder_key",
    "git_remote",
    "lookup_key",
    "normalize_remote",
    "resolve_binding",
    "resolve_vault",
]
