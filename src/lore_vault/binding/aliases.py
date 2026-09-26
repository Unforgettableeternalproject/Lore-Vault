"""vault key／別名 → Vault 解析。

repo 改名後，舊 key（或舊 repo 名）要能找到同一個 vault
（A7：歸屬凍結、讀取端用別名接起來）。
`Vault` 建構時已把 key 與 aliases 正規化成小寫，這裡只需對查詢輸入做同樣處理。

同一個 key／別名被兩個 vault 認領時直接拋錯，不「取第一個」——
那種靜默選邊會讓讀寫落到錯的範圍而完全看不出來。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from lore_vault.schema import Vault, canonical_key


class AliasConflictError(ValueError):
    """同一個 key 或別名對應到多個 vault。"""


def lookup_key(key: str) -> str:
    """查詢輸入的正規形式：去前後空白後套用 `canonical_key`。"""
    return canonical_key(key.strip())


class VaultIndex:
    """以 key 與 aliases 建立的唯讀索引。"""

    def __init__(self, vaults: Iterable[Vault]) -> None:
        index: dict[str, Vault] = {}
        for vault in vaults:
            for name in (vault.key, *vault.aliases):
                existing = index.get(name)
                if existing is not None:
                    raise AliasConflictError(
                        f"{name!r} 同時對應 vault {existing.key!r} 與 {vault.key!r}"
                    )
                index[name] = vault
        self._index: Mapping[str, Vault] = index

    def resolve(self, key: str) -> Vault | None:
        """用 key 或任一別名找 vault；找不到回傳 None。"""
        if not key or not key.strip():
            return None
        return self._index.get(lookup_key(key))

    def __len__(self) -> int:
        return len({v.key for v in self._index.values()})


def resolve_vault(key: str, vaults: Iterable[Vault]) -> Vault | None:
    """單次解析的便利函式；重複查詢請建 `VaultIndex` 重用。"""
    return VaultIndex(vaults).resolve(key)
