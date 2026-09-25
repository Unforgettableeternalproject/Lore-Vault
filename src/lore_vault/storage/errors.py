"""儲存層例外。全部繼承 `StorageError`，上層可一次攔下。"""

from __future__ import annotations


class StorageError(Exception):
    """儲存層錯誤的共同基底。"""


class VaultRequired(StorageError, ValueError):
    """未傳 vault、傳空值，或在寫入時傳了 `"*"`。"""


class UnknownVault(StorageError, LookupError):
    """vault key（或別名）不存在；不自動建立，避免拼錯字產生幽靈範圍。"""


class VaultConflict(StorageError):
    """vault key／別名與既有資料衝突（別名重複、別名撞到其他 vault 的 key）。"""


class NotFound(StorageError, LookupError):
    """指定 vault 內找不到該筆資料。"""


class DuplicateRecord(StorageError):
    """唯一鍵已存在且內容不同（相同內容的重送視為冪等、不拋錯）。"""


class SchemaVersionError(StorageError):
    """資料庫 schema 版本比程式新，或遷移清單有缺漏。"""


class DimensionMismatch(StorageError, ValueError):
    """向量維度與設定不符，或向量本身不合法（非有限值、零向量）。"""
