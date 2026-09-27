"""`download` 工具（stdio）的本機寫入路徑檢查與寫檔。

與 `upload` 對稱，是殼唯一會寫本機檔案的介面，安全邊界在這裡：
- 路徑規則同 `upload`：絕對或相對於殼工作目錄；任何一段是 `..` 直接拒絕；
  Windows 另拒絕會改變解析基準的形式（`upload.windows_path_problem`）
- 目標可以不存在：以 realpath 解開 symlink／junction 後，目標與其**父目錄**都必須
  落在白名單（`upload_roots`）之下；父目錄必須已存在（不自動建目錄）
- 路徑是既有目錄時，寫到該目錄下、以文件檔名命名；省略路徑則寫到殼工作目錄
- 服務給的檔名只當單純檔名用：含分隔符、控制字元、`:`、`.`／`..`、Windows 保留
  裝置名時改用由 document id 衍生的名稱
- 既有檔預設不覆寫（`open(..., "xb")` 獨占建立，沒有 check-then-act 的窗口）；
  明示覆寫時寫同目錄暫存檔再 `os.replace`（原子，不留半檔）；既有路徑不是一般檔
  一律拒絕
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePath

from . import upload as upload_paths
from .upload import UploadPathError

# Windows 保留裝置名（不分大小寫、含副檔名也算，例如 NUL.txt）
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_TMP_SUFFIX = ".lore-download.tmp"
MAX_FILENAME = 255


class DownloadPathError(UploadPathError):
    """錯誤碼沿用 upload（`path_not_allowed`、`invalid_request` 等），另有
    `file_exists`（未明示覆寫）、`not_a_file`、`parent_not_found`、`write_failed`。"""


def safe_filename(name: str | None, document_id: str) -> str:
    """服務給的檔名 → 可安全寫入的單純檔名；不合格時用 document id 衍生的名稱。"""
    fallback = "document-" + re.sub(
        r"[^0-9A-Za-z-]", "_", document_id.split(":", 1)[-1]
    )
    if not isinstance(name, str):
        return fallback
    candidate = name.strip()
    if (
        not candidate
        or candidate in (".", "..")
        or len(candidate) > MAX_FILENAME
        or any(sep in candidate for sep in ("/", "\\", ":"))
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
        or candidate.endswith((".", " "))
        or _RESERVED.match(candidate)
    ):
        return fallback
    return candidate


def _check_raw(raw: str) -> None:
    if not isinstance(raw, str) or not raw.strip():
        raise DownloadPathError("invalid_request", "path 不可為空")
    if "\x00" in raw:
        raise DownloadPathError("invalid_request", "path 含 NUL")
    if upload_paths.WINDOWS_PATHS and (
        problem := upload_paths.windows_path_problem(raw)
    ):
        raise DownloadPathError("path_not_allowed", f"{problem}：{raw!r}")
    if ".." in PurePath(raw.replace("\\", "/")).parts:
        raise DownloadPathError("path_not_allowed", f"路徑不可含 '..'：{raw!r}")


def _allowed(real: str, roots: Sequence[str | os.PathLike[str]]) -> bool:
    allowed = [upload_paths._norm(str(root)) for root in roots]
    target = os.path.normcase(real)
    return any(upload_paths._within(target, root) for root in allowed)


def resolve_download_path(
    raw: str | None,
    roots: Sequence[str | os.PathLike[str]],
    *,
    cwd: str,
    filename: str,
) -> Path:
    """回傳要寫入的實際路徑（realpath）。不建立任何檔案或目錄。"""
    if raw is None:
        candidate = Path(cwd) / filename
    else:
        _check_raw(raw)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        if Path(os.path.realpath(candidate)).is_dir():
            candidate = candidate / filename
    shown = raw if raw is not None else filename
    parent_real = os.path.realpath(candidate.parent)
    target_real = os.path.realpath(candidate)
    for real in (parent_real, target_real):
        if not _allowed(real, roots):
            raise DownloadPathError(
                "path_not_allowed",
                f"{shown!r} 不在允許寫入的目錄內（解析後為 {real}）",
            )
    if not Path(parent_real).is_dir():
        raise DownloadPathError(
            "parent_not_found", f"目的目錄不存在（不會自動建立）：{parent_real}"
        )
    target = Path(target_real)
    if target.exists() and not target.is_file():
        raise DownloadPathError(
            "not_a_file", f"目的路徑已存在且不是一般檔案：{shown!r}"
        )
    return target


def write_download(target: Path, data: bytes, *, overwrite: bool) -> bool:
    """寫入 `target`；回傳是否覆寫了既有檔。未明示覆寫且檔案已存在 → `file_exists`。"""
    if not overwrite:
        try:
            fh = target.open("xb")
        except FileExistsError:
            raise DownloadPathError(
                "file_exists", f"檔案已存在：{target}（要覆寫請明示 overwrite=true）"
            ) from None
        except OSError as exc:
            raise DownloadPathError(
                "write_failed", f"無法寫入 {target}：{exc}"
            ) from None
        try:
            with fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise DownloadPathError(
                "write_failed", f"無法寫入 {target}：{exc}"
            ) from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return False
    existed = target.exists()
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}{_TMP_SUFFIX}"
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise DownloadPathError("write_failed", f"無法寫入 {target}：{exc}") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return existed
