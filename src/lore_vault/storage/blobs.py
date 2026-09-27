"""文件原始檔的 blob 儲存（A19，T-59）：sha256 內容定址、去重、原子寫入。

- 路徑 `<root>/<sha256 前 2 碼>/<sha256>`；跨 vault／space 共用同一份（去重全域生效）。
  sha256 先驗格式再組路徑，外部輸入不可能組出 root 以外的路徑。
- 寫入：同目錄暫存檔（`.<sha256>.<隨機>.tmp`）→ flush + fsync → `os.replace`。
  暫存檔與目標同一個檔案系統，replace 是原子的；中途失敗刪暫存檔，不留半檔。
- 目標已存在時讀回驗雜湊：一致就不寫（去重）；不一致（損毀）就以新內容覆蓋並回報。
- 讀取一律驗雜湊，不符拋 `BlobCorrupt`，不回傳可能損毀的內容。
- 寫入路徑不刪 blob：孤兒 blob 由 doctor `documents.orphan_blobs` 回報，清理是另一個
  明確的管理操作 `cli.admin gc-blobs`（設計 3.2、風險 R-5；`plan_gc`／`execute_gc`）。
  兩者共用 `scan_orphans` 的判定。去重命中時刷新 mtime，gc 的年齡門檻才擋得住
  「舊孤兒被重新上傳、DB 交易尚未提交」的競態。

純標準庫。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .checks import MAX_DETAILS, Reconciliation
from .db import transaction
from .documents import referenced_sha256, sha256_is_referenced, validate_sha256
from .errors import StorageError

TMP_SUFFIX = ".tmp"
_READ_CHUNK = 1024 * 1024
# 暫存檔超過這個秒數仍在，視為中斷遺留（doctor warn）；較新的可能是正在寫入
STALE_TMP_SECONDS = 3600.0
# put() 產生的暫存檔名：`.<sha256>.<uuid4 hex>.tmp`
_TMP_NAME = re.compile(r"^\.([0-9a-f]{64})\.[0-9a-f]{32}\.tmp$")
_SHARD_NAME = re.compile(r"^[0-9a-f]{2}$")


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
                # 刷新 mtime：舊孤兒被重新引用時，gc 的年齡門檻才會把它當成剛寫入
                os.utime(target)
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


@dataclass(frozen=True)
class OrphanScan:
    """`scan_orphans` 的結果；doctor 與 gc 共用這份判定。"""

    total: int
    orphans: tuple[BlobEntry, ...]
    unexpected: tuple[Path, ...]
    stale_temp: tuple[Path, ...]


def scan_orphans(
    conn: sqlite3.Connection,
    store: BlobStore,
    *,
    now: float | None = None,
    stale_seconds: float = STALE_TMP_SECONDS,
) -> OrphanScan:
    """孤兒判定：blob 沒有任何 documents 列引用（不分狀態；墓碑不算引用）。

    另回不符佈局的檔案，以及 mtime 超過 `stale_seconds` 的暫存檔。
    """
    referenced = referenced_sha256(conn)
    orphans: list[BlobEntry] = []
    unexpected: list[Path] = []
    total = 0
    for entry in store.entries():
        total += 1
        if entry.sha256 is None:
            unexpected.append(entry.path)
        elif entry.sha256 not in referenced:
            orphans.append(entry)
    moment = time.time() if now is None else now
    stale = [
        p for p in store.temp_files() if moment - p.stat().st_mtime > stale_seconds
    ]
    return OrphanScan(total, tuple(orphans), tuple(unexpected), tuple(stale))


def orphan_blobs(
    conn: sqlite3.Connection, store: BlobStore, *, now: float | None = None
) -> Reconciliation:
    """blob 目錄下沒有任何 document 引用的檔案（warn，不自動刪；清理用 gc-blobs）。

    不符合佈局的檔案與中斷遺留的暫存檔也一併回報。
    """
    scan = scan_orphans(conn, store, now=now)
    orphans = [f"blob {e.sha256} 沒有 document 引用" for e in scan.orphans]
    unexpected = [f"不符合佈局的檔案：{p}" for p in scan.unexpected]
    stale = [f"中斷遺留的暫存檔：{p}" for p in scan.stale_temp]
    counts = {
        "blobs": scan.total,
        "orphans": len(orphans),
        "unexpected": len(unexpected),
        "stale_temp": len(stale),
    }
    if not orphans and not unexpected and not stale:
        return Reconciliation(
            "pass", f"{scan.total} 個 blob 皆有 document 引用", counts
        )
    return Reconciliation(
        "warn",
        f"{len(orphans)} 個孤兒 blob、{len(unexpected)} 個不明檔案、"
        f"{len(stale)} 個遺留暫存檔（不自動清理）",
        counts,
        tuple((orphans + unexpected + stale)[:MAX_DETAILS]),
    )


# ── 孤兒清理（管理指令 gc-blobs）─────────────────────────────────────


@dataclass(frozen=True)
class GcCandidate:
    """將刪的檔案。`sha256` 為 None 代表暫存檔。"""

    path: Path
    sha256: str | None
    size: int
    mtime: float


@dataclass(frozen=True)
class GcPlan:
    min_age_seconds: float
    temp_age_seconds: float
    total: int
    orphans: tuple[GcCandidate, ...]
    temps: tuple[GcCandidate, ...]
    # 孤兒但年齡未達門檻（可能是 DB 交易尚未提交的新 blob）
    young_orphans: int
    # 不符佈局的檔案：只報告，一律不碰
    unexpected: tuple[Path, ...]

    def to_dict(self, root: Path) -> dict[str, object]:
        return {
            "min_age_hours": self.min_age_seconds / 3600,
            "counts": {
                "blobs": self.total,
                "orphans": len(self.orphans),
                "orphan_bytes": sum(c.size for c in self.orphans),
                "young_orphans_kept": self.young_orphans,
                "stale_temp": len(self.temps),
                "stale_temp_bytes": sum(c.size for c in self.temps),
                "unexpected": len(self.unexpected),
            },
            # 只列雜湊前 12 碼，不讀內容
            "orphans": [c.sha256[:12] for c in self.orphans if c.sha256],
            "unexpected": [_relative(p, root) for p in self.unexpected],
        }


@dataclass(frozen=True)
class GcResult:
    deleted: int
    deleted_bytes: int
    temps_deleted: int
    temps_deleted_bytes: int
    # 刪除前再確認後留下的：已被引用、檔案被改動（mtime／大小）或年齡不足、已不存在
    kept_referenced: int
    kept_changed: int
    missing: int
    failed: tuple[str, ...]
    removed_dirs: int

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted": {
                "orphans": self.deleted,
                "orphan_bytes": self.deleted_bytes,
                "stale_temp": self.temps_deleted,
                "stale_temp_bytes": self.temps_deleted_bytes,
                "empty_dirs": self.removed_dirs,
            },
            "kept": {
                "referenced": self.kept_referenced,
                "changed": self.kept_changed,
                "missing": self.missing,
            },
            "failed": list(self.failed),
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _is_temp_layout(store: BlobStore, path: Path) -> bool:
    match = _TMP_NAME.match(path.name)
    return (
        match is not None
        and path.parent.parent == store.root
        and path.parent.name == match.group(1)[:2]
    )


def plan_gc(
    conn: sqlite3.Connection,
    store: BlobStore,
    *,
    min_age_seconds: float,
    now: float | None = None,
) -> GcPlan:
    """規劃清理（不動任何檔案、不開寫交易、不讀 blob 內容）。

    孤兒判定同 doctor（`scan_orphans`）；只收 mtime 超過 `min_age_seconds` 的孤兒。
    暫存檔門檻取 `max(min_age_seconds, STALE_TMP_SECONDS)`：門檻設 0 也不會刪到
    正在寫入的暫存檔。
    """
    if min_age_seconds < 0:
        raise ValueError("min_age_seconds 不可為負")
    moment = time.time() if now is None else now
    temp_age = max(min_age_seconds, STALE_TMP_SECONDS)
    scan = scan_orphans(conn, store, now=moment, stale_seconds=temp_age)
    orphans: list[GcCandidate] = []
    young = 0
    for entry in scan.orphans:
        st = entry.path.stat()
        if moment - st.st_mtime < min_age_seconds:
            young += 1
            continue
        orphans.append(GcCandidate(entry.path, entry.sha256, st.st_size, st.st_mtime))
    temps: list[GcCandidate] = []
    for path in scan.stale_temp:
        if not _is_temp_layout(store, path):
            continue
        st = path.stat()
        temps.append(GcCandidate(path, None, st.st_size, st.st_mtime))
    return GcPlan(
        min_age_seconds,
        temp_age,
        scan.total,
        tuple(orphans),
        tuple(temps),
        young,
        scan.unexpected,
    )


def _still_eligible(
    path: Path, cand: GcCandidate, min_age: float, moment: float
) -> bool | None:
    """重新 stat：None＝已不存在；False＝被改動或年齡不足。"""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (
        st.st_mtime == cand.mtime
        and st.st_size == cand.size
        and moment - st.st_mtime >= min_age
    )


def execute_gc(
    conn: sqlite3.Connection,
    store: BlobStore,
    plan: GcPlan,
    *,
    now: float | None = None,
) -> GcResult:
    """依規劃刪檔。整段持有 DB 寫鎖（BEGIN IMMEDIATE），鎖內逐檔：

    1. 路徑以 `store.path_for(sha)` 重算，必須與規劃一致（只刪符合佈局的檔）
    2. 同一連線再確認仍無 documents 列引用（防規劃後新增引用）
    3. 重新 stat：mtime／大小變動或年齡不足就不刪（去重命中會刷新 mtime）

    持鎖期間其他寫者無法提交新的 documents 列，所以 2 與刪除之間沒有競態窗口。
    """
    moment = time.time() if now is None else now
    deleted = deleted_bytes = temps_deleted = temps_bytes = 0
    kept_referenced = kept_changed = missing = 0
    failed: list[str] = []
    touched_dirs: set[Path] = set()

    with transaction(conn):
        for cand in plan.orphans:
            sha = cand.sha256
            if sha is None or store.path_for(sha) != cand.path:
                failed.append(f"不符佈局，未刪：{_relative(cand.path, store.root)}")
                continue
            if sha256_is_referenced(conn, sha):
                kept_referenced += 1
                continue
            state = _still_eligible(cand.path, cand, plan.min_age_seconds, moment)
            if state is None:
                missing += 1
                continue
            if not state:
                kept_changed += 1
                continue
            try:
                cand.path.unlink()
            except FileNotFoundError:
                missing += 1
                continue
            except OSError as exc:
                failed.append(f"blob {sha[:12]} 刪除失敗：{exc.strerror or exc}")
                continue
            deleted += 1
            deleted_bytes += cand.size
            touched_dirs.add(cand.path.parent)
        for cand in plan.temps:
            name = _relative(cand.path, store.root)
            if not _is_temp_layout(store, cand.path):
                failed.append(f"不符佈局，未刪：{name}")
                continue
            state = _still_eligible(cand.path, cand, plan.temp_age_seconds, moment)
            if state is None:
                missing += 1
                continue
            if not state:
                kept_changed += 1
                continue
            try:
                cand.path.unlink()
            except FileNotFoundError:
                missing += 1
                continue
            except OSError as exc:
                failed.append(f"暫存檔 {name} 刪除失敗：{exc.strerror or exc}")
                continue
            temps_deleted += 1
            temps_bytes += cand.size
            touched_dirs.add(cand.path.parent)

    removed_dirs = 0
    for directory in sorted(touched_dirs):
        if directory.parent != store.root or not _SHARD_NAME.match(directory.name):
            continue
        # 目錄非空或被占用（Windows）就留著，不讓整體失敗
        with contextlib.suppress(OSError):
            directory.rmdir()
            removed_dirs += 1
    return GcResult(
        deleted,
        deleted_bytes,
        temps_deleted,
        temps_bytes,
        kept_referenced,
        kept_changed,
        missing,
        tuple(failed),
        removed_dirs,
    )
