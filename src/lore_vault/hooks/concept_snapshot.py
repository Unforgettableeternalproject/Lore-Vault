"""concept 快照檔（T-40）：MCP 殼寫、PreToolUse hook 讀、doctor 對帳。

- 快照檔：與 spike `concepts.json` 同格式（JSON 陣列，每筆一個 concept 物件），
  內容即服務端 `GET /v1/concepts/export` 的回應本體，逐位元組寫入
- manifest：同目錄 `<檔名>.manifest.json`，記 sha256（＝服務端 ETag）、筆數、
  下載時間 `fetched_at` 與最近一次向服務確認仍是最新的時間 `checked_at`（含 304）

寫入一律同目錄暫存檔 → 驗證 → `os.replace`；驗證不過不動舊檔。
PreToolUse 每次編輯都跑，讀取端只做一次 `read_text` + `json.loads`，不碰 manifest。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_SUFFIX = ".manifest.json"


class ConceptSnapshotError(ValueError):
    """快照內容或 manifest 不符格式。訊息不含 concept 原文。"""


def manifest_path(path: Path) -> Path:
    return path.with_name(path.name + MANIFEST_SUFFIX)


def _iso(ts: datetime) -> str:
    return (
        ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ts.microsecond // 1000:03d}Z"
    )


def parse_concepts(data: bytes) -> list[dict[str, Any]]:
    """驗證是 concept 物件的陣列；不符拋 `ConceptSnapshotError`。"""
    try:
        concepts = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConceptSnapshotError(f"不是 UTF-8 JSON（{type(exc).__name__}）") from None
    if not isinstance(concepts, list):
        raise ConceptSnapshotError(f"頂層必須是陣列，得到 {type(concepts).__name__}")
    for index, item in enumerate(concepts):
        if not isinstance(item, dict):
            raise ConceptSnapshotError(f"第 {index} 筆不是物件")
    return concepts


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class ConceptManifest:
    sha256: str
    concepts: int
    fetched_at: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "concepts": self.concepts,
            "fetched_at": self.fetched_at,
            "checked_at": self.checked_at,
        }


def read_manifest(path: Path) -> ConceptManifest:
    """讀 manifest；不存在拋 `FileNotFoundError`，格式不符拋 `ConceptSnapshotError`。"""
    raw = manifest_path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        return ConceptManifest(
            sha256=str(data["sha256"]),
            concepts=int(data["concepts"]),
            fetched_at=str(data["fetched_at"]),
            checked_at=str(data["checked_at"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ConceptSnapshotError(
            f"manifest 格式不符（{type(exc).__name__}）"
        ) from None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_etag(path: Path) -> str | None:
    """本地快照與 manifest 一致時回傳 sha256（拿來帶 If-None-Match），否則 None。"""
    try:
        manifest = read_manifest(path)
        return manifest.sha256 if file_sha256(path) == manifest.sha256 else None
    except (OSError, ConceptSnapshotError):
        return None


def install(
    path: Path,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    now: datetime | None = None,
) -> ConceptManifest:
    """驗證後原子寫入快照與 manifest。`expected_sha256`（ETag）給了就必須相符。"""
    concepts = parse_concepts(data)
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ConceptSnapshotError("內容 sha256 與服務端 ETag 不符")
    stamp = _iso(now or datetime.now(UTC))
    manifest = ConceptManifest(digest, len(concepts), stamp, stamp)
    _write_atomic(path, data)
    _write_atomic(
        manifest_path(path),
        json.dumps(manifest.to_dict(), ensure_ascii=False).encode("utf-8"),
    )
    return manifest


def mark_checked(path: Path, *, now: datetime | None = None) -> ConceptManifest:
    """服務回 304：快照未變，只更新 `checked_at`。"""
    manifest = read_manifest(path)
    updated = ConceptManifest(
        manifest.sha256,
        manifest.concepts,
        manifest.fetched_at,
        _iso(now or datetime.now(UTC)),
    )
    _write_atomic(
        manifest_path(path),
        json.dumps(updated.to_dict(), ensure_ascii=False).encode("utf-8"),
    )
    return updated


def load_for_injection(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """PreToolUse 用：回傳 (concepts, 降級原因)。缺檔／損毀時 ([], 原因)，不拋例外。"""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], "concept 快照不存在"
    except OSError as exc:
        return [], f"concept 快照讀取失敗（{type(exc).__name__}）"
    try:
        return parse_concepts(data), None
    except ConceptSnapshotError as exc:
        return [], f"concept 快照損毀：{exc}"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("缺少時區")
    return parsed


@dataclass(frozen=True)
class SnapshotCheck:
    status: str  # "pass" / "fail"
    summary: str
    age_seconds: float | None = None
    concepts: int | None = None


def check_age(path: Path, *, now: datetime, max_age_seconds: float) -> SnapshotCheck:
    """doctor 用：從未拉取、manifest 與檔案不一致、內容不符格式、
    超過年齡門檻皆 fail。"""
    try:
        manifest = read_manifest(path)
    except FileNotFoundError:
        return SnapshotCheck("fail", "從未拉取 concept 快照（manifest 不存在）")
    except (OSError, ConceptSnapshotError) as exc:
        return SnapshotCheck("fail", f"manifest 無法讀取：{exc}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return SnapshotCheck("fail", f"快照檔無法讀取（{type(exc).__name__}）")
    if hashlib.sha256(data).hexdigest() != manifest.sha256:
        return SnapshotCheck(
            "fail", "快照檔 sha256 與 manifest 不一致（被改動或寫到一半）"
        )
    try:
        concepts = parse_concepts(data)
    except ConceptSnapshotError as exc:
        return SnapshotCheck("fail", f"快照內容不符格式：{exc}")
    try:
        age = (now - _parse_utc(manifest.checked_at)).total_seconds()
    except ValueError:
        return SnapshotCheck("fail", "manifest checked_at 不是 UTC 時間")
    if age > max_age_seconds:
        return SnapshotCheck(
            "fail",
            f"concept 快照 {age / 3600:.1f} 小時未更新"
            f"（門檻 {max_age_seconds / 3600:g}）",
            age,
            len(concepts),
        )
    return SnapshotCheck(
        "pass", f"{len(concepts)} 條，{age / 60:.0f} 分鐘前確認", age, len(concepts)
    )
