"""唯讀快照（T-31）：服務端產生、客戶端（MCP 殼）安裝與對帳。

服務端 `build_snapshot`：
- 新檔以 `migrate` 建出完整 schema（`user_version` 與程式一致），再 ATTACH 來源（唯讀）
  在**同一個交易**內複製白名單資料表：`vaults`、`vault_aliases`、`notes`、`note_fts`。
  同一交易內跨表讀取落在同一個 WAL 讀取快照上，不會拿到半套寫入
- 白名單而非「整庫複製再刪」：episode／concept／injection 含對話原文，新增資料表
  也不會因為忘了刪而被送到其他機器。向量不帶（降級只走 lexical，省下大半體積）
- 文件（T-69）明確排除（`SNAPSHOT_EXCLUDED_TABLES`）：文件檢索只走線上服務，降級時
  recall／get／list 把文件標成不支援而不是讀空表
- 快照是單一檔（rollback journal，非 WAL），客戶端以 `mode=ro` 開啟

客戶端：
- `install_snapshot`：驗證暫存檔（integrity、schema 版本、sha256、筆數）→ `os.replace`
  成 `snapshot.db` → manifest 同樣暫存 + replace。任何一步失敗都刪暫存檔，舊快照不動
- `snapshot_state`：doctor 與降級讀取共用的狀態讀取（manifest 與實檔是否一致）

只用標準庫與本套件的 storage／schema。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from os import PathLike
from pathlib import Path

from lore_vault.schema import SchemaError

from .checks import Reconciliation
from .db import connect_readonly
from .errors import StorageError
from .migrate import SCHEMA_VERSION, current_version, migrate
from .timeutil import parse_utc, utc_now

SNAPSHOT_DB_NAME = "snapshot.db"
MANIFEST_NAME = "snapshot.json"
MEDIA_TYPE = "application/vnd.sqlite3"
PARTIAL_SUFFIX = ".partial"

# 快照複製的資料表（白名單；`build_snapshot` 只複製這些）
SNAPSHOT_TABLES = ("vaults", "vault_aliases", "notes", "note_fts")
# 明確不進快照的表（守門測試：這些表不可出現在白名單，快照裡必為空）
SNAPSHOT_EXCLUDED_TABLES = (
    "note_embeddings",
    "note_enrichment",
    "episodes",
    "concepts",
    "injections",
    "documents",
    "document_chunks",
    "chunk_fts",
    "document_chunk_embeddings",
    "document_tombstones",
    "document_enrichment",
    # 側載機器狀態（schema v17）只經 blob_get 讀取，不進降級快照
    "sidecar_blobs",
)

# HTTP header（服務端回應 `GET /v1/snapshot` 時帶）
HEADER_GENERATED_AT = "X-Lore-Vault-Snapshot-Generated-At"
HEADER_SCHEMA_VERSION = "X-Lore-Vault-Schema-Version"
HEADER_SERVICE_VERSION = "X-Lore-Vault-Service-Version"
HEADER_SHA256 = "X-Lore-Vault-Snapshot-Sha256"
HEADER_NOTES = "X-Lore-Vault-Snapshot-Notes"

# Windows 上目標檔被其他程序開著時 os.replace 會 PermissionError；短暫重試
_REPLACE_RETRIES = 5
_REPLACE_DELAY = 0.2


class SnapshotError(StorageError):
    """快照產生、驗證或安裝失敗；失敗時不留半檔、不動舊快照。"""


@dataclass(frozen=True)
class SnapshotInfo:
    """服務端產生的一份快照。"""

    generated_at: str
    schema_version: int
    notes: int
    bytes: int
    sha256: str
    # 快照內容的指紋（`content_fingerprint`）；服務端快取以此判斷資料是否變動
    fingerprint: str = ""


@dataclass(frozen=True)
class Manifest:
    """客戶端 manifest（`snapshot.json`）：快照來源與拉取時間。"""

    generated_at: str
    schema_version: int
    service_version: str
    sha256: str
    bytes: int
    notes: int
    pulled_at: str
    # 最近一次向服務確認快照仍是最新（200 下載或 304 未變）的時間；年齡以此計
    checked_at: str | None = None

    @property
    def fresh_as_of(self) -> str:
        return self.checked_at or self.pulled_at


def file_sha256(path: str | PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── 服務端 ──────────────────────────────────────────────────────────

# 指紋涵蓋快照白名單中的原始資料表（note_fts 由 notes 衍生，不另算）。
# 用內容雜湊而非「max(updated) + 筆數」：背景補摘要不改 updated、刪一筆再寫一筆
# 筆數不變，這類變動都必須讓快取失效。`PRAGMA data_version` 只在同一條連線內有意義，
# 檔案標頭的 change counter 在 WAL 模式不更新，兩者都不可靠。
_FINGERPRINT_QUERIES = (
    "SELECT key, display, kind, created, space FROM {db}.vaults ORDER BY key",
    "SELECT alias, vault FROM {db}.vault_aliases ORDER BY alias",
    "SELECT seq, id, vault, title, summary, body, topics, links, supersedes, "
    "created, updated, author, principal, updated_by, updated_by_principal "
    "FROM {db}.notes ORDER BY seq",
)


def content_fingerprint(conn: sqlite3.Connection, db: str = "main") -> str:
    """快照白名單資料的內容雜湊（含 schema 版本）。呼叫端負責讀取一致性。"""
    digest = hashlib.sha256(f"schema:{SCHEMA_VERSION}\n".encode())
    for query in _FINGERPRINT_QUERIES:
        digest.update(b"\x1e")
        for row in conn.execute(query.format(db=db)):
            digest.update(json.dumps(list(row), ensure_ascii=False).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def source_fingerprint(source_db: str | PathLike[str]) -> str:
    """live 資料庫目前的指紋（唯讀連線、單一讀取交易）。"""
    conn = connect_readonly(source_db)
    try:
        conn.execute("BEGIN")
        try:
            return content_fingerprint(conn)
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


def build_snapshot(
    source_db: str | PathLike[str], dest: str | PathLike[str]
) -> SnapshotInfo:
    """由來源資料庫產生快照到 `dest`（不可已存在）。失敗時刪掉 `dest`。"""
    source = Path(source_db)
    target = Path(dest)
    if not source.is_file():
        raise SnapshotError(f"來源資料庫不存在：{source}")
    if target.exists():
        raise SnapshotError(f"快照目的檔已存在：{target}")
    generated_at = utc_now()
    # URI 模式：ATTACH 來源時才能帶 `?mode=ro`
    conn = sqlite3.connect(
        f"{target.resolve().as_uri()}?mode=rwc", uri=True, isolation_level=None
    )
    try:
        try:
            migrate(conn)
            conn.execute(
                "ATTACH DATABASE ? AS src", (f"{source.resolve().as_uri()}?mode=ro",)
            )
            src_version = int(conn.execute("PRAGMA src.user_version").fetchone()[0])
            if src_version != SCHEMA_VERSION:
                raise SnapshotError(
                    f"來源 schema 版本 {src_version} 與程式預期 {SCHEMA_VERSION} 不符"
                )
            conn.execute("BEGIN")
            conn.execute(
                # space 必須一起複製：否則快照裡的 vault 全落回預設 dev，
                # 降級時 lore／personal 的內容會在 dev 下被看見
                "INSERT INTO main.vaults (key, display, kind, created, space) "
                "SELECT key, display, kind, created, space FROM src.vaults"
            )
            conn.execute(
                "INSERT INTO main.vault_aliases (alias, vault) "
                "SELECT alias, vault FROM src.vault_aliases"
            )
            conn.execute(
                """
                INSERT INTO main.notes (seq, id, vault, title, summary, body, topics,
                                        links, supersedes, created, updated,
                                        author, principal, updated_by,
                                        updated_by_principal)
                SELECT seq, id, vault, title, summary, body, topics,
                       links, supersedes, created, updated,
                       author, principal, updated_by, updated_by_principal
                FROM src.notes
                """
            )
            conn.execute(
                "INSERT INTO main.note_fts (rowid, title, content) "
                "SELECT rowid, title, content FROM src.note_fts"
            )
            conn.execute("COMMIT")
            conn.execute("DETACH DATABASE src")
            _assert_excluded_empty(conn)
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                raise SnapshotError(f"快照 integrity_check 失敗：{rows[:5]}")
            notes = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
            # 從快照本身算：指紋與檔案內容嚴格對應（不受建置期間的新寫入影響）
            fingerprint = content_fingerprint(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise SnapshotError(f"產生快照失敗：{exc}") from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return SnapshotInfo(
        generated_at=generated_at,
        schema_version=SCHEMA_VERSION,
        notes=notes,
        bytes=target.stat().st_size,
        sha256=file_sha256(target),
        fingerprint=fingerprint,
    )


def _assert_excluded_empty(conn: sqlite3.Connection) -> None:
    """白名單以外的表在快照裡必須是空的（含文件，T-69）；否則拒絕產生快照。"""
    leaked = []
    for table in SNAPSHOT_EXCLUDED_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'", (table,)
        ).fetchone()
        if exists and conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]:
            leaked.append(table)
    if leaked:
        raise SnapshotError(f"快照含不應帶出的資料表內容：{leaked}")


def etag(sha256: str) -> str:
    return f'"{sha256}"'


def etag_matches(if_none_match: str | None, sha256: str) -> bool:
    """解析 If-None-Match（可多值、可帶 W/）；`*` 也算符合。"""
    if not if_none_match:
        return False
    for raw in if_none_match.split(","):
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag.strip('"') == sha256:
            return True
    return False


class SnapshotCache:
    """服務端快照快取：資料指紋未變就沿用上一份，不重建。

    檢查、重建、讀檔都在同一把鎖內：並行請求不會重複建置，
    讀檔時舊檔也不會被下一次重建刪掉。
    """

    def __init__(self, source_db: str | PathLike[str], cache_dir: Path) -> None:
        self.source_db = Path(source_db)
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._current: SnapshotInfo | None = None
        self._path: Path | None = None
        self._seq = 0
        self.builds = 0

    def get(
        self, if_none_match: str | None = None
    ) -> tuple[SnapshotInfo, bytes | None]:
        """回傳（快照資訊, 內容）；If-None-Match 符合時內容為 None（回 304）。"""
        with self._lock:
            fingerprint = source_fingerprint(self.source_db)
            current, path = self._current, self._path
            if (
                current is None
                or path is None
                or current.fingerprint != fingerprint
                or not path.is_file()
            ):
                current, path = self._rebuild()
            if etag_matches(if_none_match, current.sha256):
                return current, None
            return current, path.read_bytes()

    def _rebuild(self) -> tuple[SnapshotInfo, Path]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        path = self.cache_dir / f"snapshot-{os.getpid()}-{self._seq}.db"
        path.unlink(missing_ok=True)
        info = build_snapshot(self.source_db, path)
        old = self._path
        self._current, self._path = info, path
        self.builds += 1
        if old is not None and old != path:
            old.unlink(missing_ok=True)
        return info, path


# ── 客戶端 ──────────────────────────────────────────────────────────


def verify_snapshot(path: str | PathLike[str], *, expected_version: int) -> int:
    """唯讀開啟快照，驗證 integrity 與 schema 版本；回傳 note 數。"""
    target = Path(path)
    try:
        conn = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise SnapshotError(f"快照無法開啟：{target.name}（{exc}）") from None
    try:
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            version = current_version(conn)
            notes = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(
                f"快照不是有效的資料庫：{target.name}（{exc}）"
            ) from None
    finally:
        conn.close()
    if rows != ["ok"]:
        raise SnapshotError(f"快照 integrity_check 失敗：{rows[:5]}")
    if version != expected_version:
        raise SnapshotError(
            f"快照 schema 版本 {version} 與預期 {expected_version} 不符"
        )
    return notes


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # Windows：目標檔正被讀取（降級查詢、其他殼）；稍後重試
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY)


def _write_manifest(snapshot_dir: Path, manifest: Manifest) -> None:
    tmp = snapshot_dir / f".{MANIFEST_NAME}.{os.getpid()}{PARTIAL_SUFFIX}"
    try:
        tmp.write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _replace(tmp, snapshot_dir / MANIFEST_NAME)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def install_snapshot(
    partial: str | PathLike[str],
    snapshot_dir: str | PathLike[str],
    *,
    generated_at: str,
    schema_version: int,
    service_version: str,
    sha256: str,
    notes: int | None = None,
) -> Manifest:
    """驗證暫存快照並原子替換成正式快照。任何失敗都刪掉暫存檔、舊快照不動。

    `schema_version`／`sha256`／`notes` 來自服務端 header；`schema_version` 必須等於
    本程式的 `SCHEMA_VERSION`（舊殼不可拿新 schema 的快照硬查）。
    """
    tmp = Path(partial)
    dest = Path(snapshot_dir)
    try:
        try:
            parse_utc(generated_at)
        except (SchemaError, ValueError) as exc:
            raise SnapshotError(f"快照產生時間格式錯誤：{exc}") from None
        if schema_version != SCHEMA_VERSION:
            raise SnapshotError(
                f"服務端快照 schema 版本 {schema_version} 與本程式預期 "
                f"{SCHEMA_VERSION} 不符（殼或服務需要升級）"
            )
        actual_sha = file_sha256(tmp)
        if actual_sha != sha256:
            raise SnapshotError("快照 sha256 與服務端宣告不符（傳輸不完整或被竄改）")
        actual_notes = verify_snapshot(tmp, expected_version=schema_version)
        if notes is not None and actual_notes != notes:
            raise SnapshotError(
                f"快照 note 數 {actual_notes} 與服務端宣告 {notes} 不符"
            )
        manifest = Manifest(
            generated_at=generated_at,
            schema_version=schema_version,
            service_version=service_version,
            sha256=actual_sha,
            bytes=tmp.stat().st_size,
            notes=actual_notes,
            pulled_at=(now := utc_now()),
            checked_at=now,
        )
        _replace(tmp, dest / SNAPSHOT_DB_NAME)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _write_manifest(dest, manifest)
    return manifest


def mark_checked(snapshot_dir: str | PathLike[str], *, sha256: str) -> Manifest:
    """服務回 304（快照未變）：確認本地快照仍是該版本後更新 `checked_at`。"""
    dest = Path(snapshot_dir)
    manifest = read_manifest(dest)
    if manifest.sha256 != sha256:
        raise SnapshotError("服務回 304，但本地 manifest 的 sha256 與 ETag 不符")
    if file_sha256(dest / SNAPSHOT_DB_NAME) != sha256:
        raise SnapshotError("服務回 304，但本地快照檔已被改動")
    updated = dataclasses.replace(manifest, checked_at=utc_now())
    _write_manifest(dest, updated)
    return updated


def local_sha256(snapshot_dir: str | PathLike[str]) -> str | None:
    """可用於 If-None-Match 的本地快照 sha256：manifest 與實檔一致才回傳。"""
    dest = Path(snapshot_dir)
    try:
        manifest = read_manifest(dest)
        actual = file_sha256(dest / SNAPSHOT_DB_NAME)
    except (SnapshotError, OSError):
        return None
    return actual if actual == manifest.sha256 else None


def read_manifest(snapshot_dir: str | PathLike[str]) -> Manifest:
    path = Path(snapshot_dir) / MANIFEST_NAME
    if not path.is_file():
        raise SnapshotError(f"尚未拉取快照（{path} 不存在）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = Manifest(**data)
        parse_utc(manifest.generated_at)
    except (OSError, ValueError, TypeError, SchemaError) as exc:
        raise SnapshotError(f"{MANIFEST_NAME} 無法解析：{exc}") from None
    return manifest


def open_snapshot(
    snapshot_dir: str | PathLike[str],
) -> tuple[sqlite3.Connection, Manifest]:
    """唯讀開啟快照供降級查詢。schema 與本程式不符時拒絕（不硬查）。

    呼叫端用完要立刻關閉：Windows 上開著的連線會讓下一次替換失敗。
    """
    manifest = read_manifest(snapshot_dir)
    if manifest.schema_version != SCHEMA_VERSION:
        raise SnapshotError(
            f"快照 schema 版本 {manifest.schema_version} 與本程式預期 "
            f"{SCHEMA_VERSION} 不符"
        )
    conn = connect_readonly(Path(snapshot_dir) / SNAPSHOT_DB_NAME)
    try:
        version = current_version(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise SnapshotError(f"快照無法讀取：{exc}") from None
    if version != SCHEMA_VERSION:
        conn.close()
        raise SnapshotError(
            f"快照檔 schema 版本 {version} 與本程式預期 {SCHEMA_VERSION} 不符"
        )
    return conn, manifest


# ── 對帳（doctor）─────────────────────────────────────────────────


def snapshot_schema(
    snapshot_dir: str | PathLike[str], *, expected_version: int = SCHEMA_VERSION
) -> Reconciliation:
    """manifest 與快照檔一致（sha256）、schema 版本等於程式預期。"""
    try:
        manifest = read_manifest(snapshot_dir)
    except SnapshotError as exc:
        return Reconciliation("fail", str(exc))
    db = Path(snapshot_dir) / SNAPSHOT_DB_NAME
    if not db.is_file():
        return Reconciliation("fail", f"manifest 在但快照檔不存在：{db}")
    counts = {
        "manifest_schema_version": manifest.schema_version,
        "expected_schema_version": expected_version,
    }
    if file_sha256(db) != manifest.sha256:
        return Reconciliation(
            "fail", "快照檔與 manifest 的 sha256 不一致（替換中斷或檔案被改動）", counts
        )
    try:
        conn = connect_readonly(db)
        try:
            file_version = current_version(conn)
        finally:
            conn.close()
    except (sqlite3.DatabaseError, StorageError) as exc:
        return Reconciliation("fail", f"快照檔無法讀取：{exc}", counts)
    counts["file_schema_version"] = file_version
    if manifest.schema_version != expected_version or file_version != expected_version:
        return Reconciliation(
            "fail",
            f"快照 schema 版本（manifest {manifest.schema_version}、檔案 "
            f"{file_version}）與程式預期 {expected_version} 不符",
            counts,
        )
    return Reconciliation("pass", f"schema 版本 {expected_version}", counts)


def snapshot_age(
    snapshot_dir: str | PathLike[str], *, now: datetime, max_age_seconds: float
) -> Reconciliation:
    """快照產生時間在門檻內；從未拉取、manifest 損毀、超過門檻皆 fail。"""
    try:
        manifest = read_manifest(snapshot_dir)
    except SnapshotError as exc:
        return Reconciliation("fail", str(exc))
    # 以最近一次向服務確認的時間計（304 未變也算確認過）
    age = max(0.0, (now - parse_utc(manifest.fresh_as_of)).total_seconds())
    counts = {"age_seconds": int(age), "max_age_seconds": int(max_age_seconds)}
    details = (
        f"產生於 {manifest.generated_at}，拉取於 {manifest.pulled_at}，"
        f"最近確認 {manifest.fresh_as_of}（服務 {manifest.service_version}）",
    )
    if age > max_age_seconds:
        return Reconciliation(
            "fail",
            f"快照已 {age / 3600:.1f} 小時未向服務確認，超過門檻 "
            f"{max_age_seconds / 3600:g} 小時（拉取可能一直失敗）",
            counts,
            details,
        )
    return Reconciliation(
        "pass", f"快照最近確認於 {age / 3600:.1f} 小時前", counts, details
    )
