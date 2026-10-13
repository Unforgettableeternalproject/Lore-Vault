"""stdio 殼的專案目錄來源與守門。

桌面型 MCP 客戶端啟動殼時行程 cwd 常是 `C:\\WINDOWS\\system32`，不是專案目錄。
解析順序（`Shell` 套用）：工具的顯式 `cwd` 參數 → 客戶端宣告的第一個 root →
環境變數 `LORE_VAULT_CWD` → 行程 cwd。這裡只放不依賴 `Shell` 的純函式。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

# 手動覆寫／測試用的專案目錄
CWD_ENV = "LORE_VAULT_CWD"
# 系統目錄的環境變數（Windows）
_SYSTEM_DIR_ENVS = ("SystemRoot", "WINDIR")


def root_from_uri(uri: str) -> Path | None:
    """`file://` root 轉成本機路徑（處理 Windows `file:///C:/…` 與百分比編碼）。

    不是 `file://`、或指向其他主機（非 localhost）時回 None。
    """
    parsed = urlparse(str(uri))
    if parsed.scheme.lower() != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    path = url2pathname(unquote(parsed.path))
    return Path(path) if path else None


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_system_dir(path: str | os.PathLike[str]) -> bool:
    """目錄是否落在 %WINDIR%（SystemRoot）底下或是磁碟根目錄：這種目錄不會是專案。"""
    target = _norm(path)
    if os.path.dirname(target) == target:  # 磁碟根（Windows `C:\\`、POSIX `/`）
        return True
    for name in _SYSTEM_DIR_ENVS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        system = _norm(raw)
        if target == system or target.startswith(system.rstrip(os.sep) + os.sep):
            return True
    return False
