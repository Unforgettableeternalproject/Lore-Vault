"""文件原始檔的 blob 儲存（A19，T-59）：sha256 內容定址、去重、原子寫入。

- 路徑 `<root>/<sha256 前 2 碼>/<sha256>`；跨 vault／space 共用同一份（去重全域生效）。
  sha256 先驗格式再組路徑，外部輸入不可能組出 root 以外的路徑。
- 寫入：同目錄暫存檔（`.<sha256>.<隨機>.tmp`）→ flush + fsync → `os.replace`。
  暫存檔與目標同一個檔案系統，replace 是原子的；中途失敗刪暫存檔，不留半檔。
- 目標已存在時讀回驗雜湊：一致就不寫（去重）；不一致（損毀）就以新內容覆蓋並回報。
- 讀取一律驗雜湊，不符拋 `BlobCorrupt`，不回傳可能損毀的內容。
- 不刪 blob：孤兒 blob 由 doctor `documents.orphan_blobs` 回報，清理是另一個
  明確的管理操作（設計 3.2、風險 R-5）。

純標準庫。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .checks import MAX_DETAILS, Reconciliation
from .documents import referenced_sha256, validate_sha256
from .errors import StorageError

TMP_SUFFIX = ".tmp"
_READ_CHUNK = 1024 * 1024
# 暫存檔超過這個秒數仍在，視為中斷遺留（doctor warn）；較新的可能是正在寫入
STALE_TMP_SECONDS = 3600.0


class BlobError(StorageError):
    """blob 儲存錯誤的共同基底。"""


class BlobNotFound(BlobError, LookupError):
    """指定雜湊的 blob 不存在。"""


class BlobCorrupt(BlobError):
    """blob 內容的雜湊與檔名不符（損毀或被竄改）。"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_READ_CHUNK):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class PutResult:
    sha256: str
    path: Path
    # True：這次實際寫入（新檔或修復損毀檔）；False：既有檔雜湊一致，去重略過
    written: bool
    # True：既有檔雜湊不符，已以新內容覆蓋
    repaired: bool = False


@dataclass(frozen=True)
class BlobEntry:
    """blob 目錄下的一個檔案。`sha256` 為 None 代表不符合佈局（不是本系統寫的）。"""

    path: Path
    sha256: str | None


class BlobStore:
    def __init__(self, root: str | PathLike[str]) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str) -> Path:
        sha = validate_sha256(sha256)
        return self.root / sha[:2] / sha

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put(self, data: bytes) -> PutResult:
        """寫入內容並回傳其 sha256；同內容已存在且完好時不重寫。"""
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError(f"data 必須是 bytes，得到 {type(data).__name__}")
        data = bytes(data)
        sha = sha256_bytes(data)
        target = self.path_for(sha)
        repaired = False
        if target.is_file():
            if _hash_file(target) == sha:
                return PutResult(sha, target, written=False)
            repaired = True
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{sha}.{uuid.uuid4().hex}{TMP_SUFFIX}"
        try:
            with tmp.open("xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return PutResult(sha, target, written=True, repaired=repaired)

    def verify(self, sha256: str) -> str:
        """'ok'／'missing'／'mismatch'。"""
        path = self.path_for(sha256)
        if not path.is_file():
            return "missing"
        return "ok" if _hash_file(path) == sha256 else "mismatch"

    def read(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise BlobNotFound(f"blob 不存在：{sha256}") from None
        if sha256_bytes(data) != sha256:
            raise BlobCorrupt(f"blob 內容與雜湊不符：{sha256}")
        return data

    def entries(self) -> Iterator[BlobEntry]:
        """列出 root 下所有檔案（暫存檔除外），依路徑排序。"""
        if not self.root.is_dir():
            return
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            if path.name.endswith(TMP_SUFFIX) and path.name.startswith("."):
                continue
            sha: str | None = path.name
            try:
                validate_sha256(sha)
            except ValueError:
                sha = None
            if sha is not None and path.parent != self.root / sha[:2]:
                sha = None
            yield BlobEntry(path, sha)

    def temp_files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            p
            for p in self.root.rglob(f".*{TMP_SUFFIX}")
            if p.is_file() and p.parent.parent == self.root
        )


# ── doctor 對帳（分類 documents）────────────────────────────────────


def blob_exists(conn: sqlite3.Connection, store: BlobStore) -> Reconciliation:
    """每個 document 引用的 blob 都存在，且內容雜湊與檔名一致。

    涵蓋所有狀態的 document（設計草案只查 ready；但 pending 沒有 blob 就無從抽取，
    所以一律檢查）。
    """
    rows = conn.execute(
        "SELECT sha256, count(*) FROM documents GROUP BY sha256 ORDER BY sha256"
    ).fetchall()
    missing: list[str] = []
    mismatched: list[str] = []
    for sha, refs in rows:
        state = store.verify(sha)
        if state == "missing":
            missing.append(f"blob {sha} 不存在（{refs} 個 document 引用）")
        elif state == "mismatch":
            mismatched.append(f"blob {sha} 內容雜湊不符（{refs} 個 document 引用）")
    documents = int(conn.execute("SELECT count(*) FROM documents").fetchone()[0])
    counts = {
        "documents": documents,
        "blobs_checked": len(rows),
        "missing": len(missing),
        "mismatched": len(mismatched),
    }
    if not missing and not mismatched:
        return Reconciliation(
            "pass", f"{len(rows)} 個被引用的 blob 皆存在且雜湊正確", counts
        )
    return Reconciliation(
        "fail",
        f"{len(missing)} 個 blob 遺失、{len(mismatched)} 個雜湊不符",
        counts,
        tuple((missing + mismatched)[:MAX_DETAILS]),
    )


def orphan_blobs(
    conn: sqlite3.Connection, store: BlobStore, *, now: float | None = None
) -> Reconciliation:
    """blob 目錄下沒有任何 document 引用的檔案（warn，不自動刪）。

    不符合佈局的檔案與中斷遺留的暫存檔也一併回報。
    """
    referenced = referenced_sha256(conn)
    orphans: list[str] = []
    unexpected: list[str] = []
    total = 0
    for entry in store.entries():
        total += 1
        if entry.sha256 is None:
            unexpected.append(f"不符合佈局的檔案：{entry.path}")
        elif entry.sha256 not in referenced:
            orphans.append(f"blob {entry.sha256} 沒有 document 引用")
    moment = time.time() if now is None else now
    stale = [
        f"中斷遺留的暫存檔：{p}"
        for p in store.temp_files()
        if moment - p.stat().st_mtime > STALE_TMP_SECONDS
    ]
    counts = {
        "blobs": total,
        "orphans": len(orphans),
        "unexpected": len(unexpected),
        "stale_temp": len(stale),
    }
    if not orphans and not unexpected and not stale:
        return Reconciliation("pass", f"{total} 個 blob 皆有 document 引用", counts)
    return Reconciliation(
        "warn",
        f"{len(orphans)} 個孤兒 blob、{len(unexpected)} 個不明檔案、"
        f"{len(stale)} 個遺留暫存檔（不自動清理）",
        counts,
        tuple((orphans + unexpected + stale)[:MAX_DETAILS]),
    )
