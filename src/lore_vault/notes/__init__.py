"""服務層：Notes 的 CRUD、查重、版本。

公開 API 見 `service`：`write`／`update`／`get`／`list_`／`find_duplicates`。
"""

from .service import (
    DuplicateCandidate,
    GetResult,
    InvalidCursor,
    ListResult,
    NoChanges,
    UpdateResult,
    VersionConflict,
    WriteResult,
    find_duplicates,
    get,
    list_,
    update,
    write,
)

__all__ = [
    "DuplicateCandidate",
    "GetResult",
    "InvalidCursor",
    "ListResult",
    "NoChanges",
    "UpdateResult",
    "VersionConflict",
    "WriteResult",
    "find_duplicates",
    "get",
    "list_",
    "update",
    "write",
]
