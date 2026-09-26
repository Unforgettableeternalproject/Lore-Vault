"""`upload` 工具的本機路徑檢查與讀檔（T-67，設計 6.2）。

這是殼唯一會讀本機任意路徑的介面，安全邊界在這裡：
- 路徑可為絕對或相對（相對於殼的工作目錄）；任何一段是 `..` 直接拒絕
  （即使正規化後仍在白名單內——不接受需要猜測意圖的路徑）
- 以 `os.path.realpath` 解開 symlink／junction 後，必須落在某個白名單目錄
  （同樣取 realpath）之下；逃出去回 `path_not_allowed`
- 只收一般檔案；讀的是 realpath（檢查過的那一個），不是原路徑
- 大小在讀檔時就擋：最多讀 `max_bytes + 1` 位元組
- Windows（`os.name == "nt"`）另拒絕會改變解析基準或開到非一般檔案的形式
  （`windows_path_problem`）：`C:foo`（依行程在該磁碟的目前目錄解析、丟掉 cwd）、
  `\\foo`（目前磁碟根目錄）、UNC `\\\\server\\share`、`\\\\?\\`／`\\\\.\\` 裝置前綴、
  NTFS 替代資料流（`file.txt:stream`）
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath


class UploadPathError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LocalFile:
    path: Path
    name: str
    data: bytes


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(target: str, root: str) -> bool:
    try:
        return os.path.commonpath([target, root]) == root
    except ValueError:  # 不同磁碟機
        return False


# 是否套用 Windows 路徑形式檢查（測試可替換）
WINDOWS_PATHS = os.name == "nt"


def windows_path_problem(raw: str) -> str | None:
    """Windows 路徑中不接受的形式；可接受回 None。"""
    path = PureWindowsPath(raw)
    drive = path.drive
    if drive.startswith("\\\\"):
        return r"不接受 UNC 或裝置路徑（\\server\share、\\?\、\\.\）"
    if drive and not path.is_absolute():
        return "有磁碟代號的路徑必須是絕對路徑（C:foo 會依該磁碟的目前目錄解析）"
    if path.root and not drive:
        return r"以 \ 開頭的路徑必須帶磁碟代號（會依目前磁碟的根目錄解析）"
    if ":" in str(path)[len(drive) :]:
        return "路徑不可含 ':'（NTFS 替代資料流）"
    return None


def resolve_upload_path(
    raw: str, roots: Sequence[str | os.PathLike[str]], *, cwd: str
) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise UploadPathError("invalid_request", "path 不可為空")
    if "\x00" in raw:
        raise UploadPathError("invalid_request", "path 含 NUL")
    if WINDOWS_PATHS and (problem := windows_path_problem(raw)):
        raise UploadPathError("path_not_allowed", f"{problem}：{raw!r}")
    parts = PurePath(raw.replace("\\", "/")).parts
    if ".." in parts:
        raise UploadPathError("path_not_allowed", f"路徑不可含 '..'：{raw!r}")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    real = _norm(str(candidate))
    allowed = [_norm(str(root)) for root in roots]
    if not any(_within(real, root) for root in allowed):
        raise UploadPathError(
            "path_not_allowed",
            f"{raw!r} 不在允許上傳的目錄內（解析後為 {os.path.realpath(candidate)}）",
        )
    resolved = Path(os.path.realpath(candidate))
    if not resolved.exists():
        raise UploadPathError("file_not_found", f"檔案不存在：{raw!r}")
    if not resolved.is_file():
        raise UploadPathError("not_a_file", f"不是一般檔案：{raw!r}")
    return resolved


def read_upload(
    raw: str,
    roots: Sequence[str | os.PathLike[str]],
    *,
    cwd: str,
    max_bytes: int,
) -> LocalFile:
    path = resolve_upload_path(raw, roots, cwd=cwd)
    try:
        with path.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        raise UploadPathError("read_failed", f"無法讀取 {raw!r}：{exc}") from None
    if len(data) > max_bytes:
        raise UploadPathError("too_large", f"{raw!r} 超過上傳上限 {max_bytes} 位元組")
    # 上傳用的檔名取使用者給的名字（symlink 指向的實體檔名可能不同）
    return LocalFile(path=path, name=Path(raw).name or path.name, data=data)
