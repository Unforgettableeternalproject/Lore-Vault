"""`upload` 工具的本機路徑檢查與讀檔（T-67，設計 6.2）。

這是殼唯一會讀本機任意路徑的介面，安全邊界在這裡：
- 路徑可為絕對或相對（相對於殼的工作目錄）；任何一段是 `..` 直接拒絕
  （即使正規化後仍在白名單內——不接受需要猜測意圖的路徑）
- 以 `os.path.realpath` 解開 symlink／junction 後，必須落在某個白名單目錄
  （同樣取 realpath）之下；逃出去回 `path_not_allowed`
- 只收一般檔案；讀的是 realpath（檢查過的那一個），不是原路徑
- 大小在讀檔時就擋：最多讀 `max_bytes + 1` 位元組
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath


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


def resolve_upload_path(
    raw: str, roots: Sequence[str | os.PathLike[str]], *, cwd: str
) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise UploadPathError("invalid_request", "path 不可為空")
    if "\x00" in raw:
        raise UploadPathError("invalid_request", "path 含 NUL")
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
