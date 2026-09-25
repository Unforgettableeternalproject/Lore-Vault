"""殼端快照拉取：下載到同目錄暫存檔 → 驗證 → 原子替換
（`storage.snapshot.install_snapshot`）。

同一台機器可能有多個殼（多個 Claude Code session）共用快照目錄：暫存檔名各自唯一，
替換靠 `os.replace` 的原子性；後替換者勝出，兩份都是完整快照。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from lore_vault.storage import snapshot as storage_snapshot

from .client import ServiceClient

logger = logging.getLogger("lore_vault.mcp.snapshot")


def _header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name.lower())
    if value is None:
        raise storage_snapshot.SnapshotError(f"服務回應缺少 header {name}")
    return value


def _int_header(headers: dict[str, str], name: str) -> int:
    value = _header(headers, name)
    try:
        return int(value)
    except ValueError:
        raise storage_snapshot.SnapshotError(f"header {name} 不是整數") from None


async def pull_snapshot(
    client: ServiceClient, snapshot_dir: Path
) -> storage_snapshot.Manifest:
    """拉一次快照並安裝。失敗時拋例外（`ServiceUnreachable`／`ServiceError`／
    `SnapshotError`／`OSError`），暫存檔一律刪除、舊快照不動。"""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=snapshot_dir, prefix=".snapshot-", suffix=storage_snapshot.PARTIAL_SUFFIX
    )
    os.close(fd)
    partial = Path(name)
    try:
        local = storage_snapshot.local_sha256(snapshot_dir)
        status, headers = await client.download_snapshot(partial, if_none_match=local)
        if status == 304:
            # 服務端資料未變：沿用本地快照，只更新確認時間
            if local is None:
                raise storage_snapshot.SnapshotError("未帶 ETag 卻收到 304")
            return storage_snapshot.mark_checked(
                snapshot_dir, sha256=_header(headers, storage_snapshot.HEADER_SHA256)
            )
        return storage_snapshot.install_snapshot(
            partial,
            snapshot_dir,
            generated_at=_header(headers, storage_snapshot.HEADER_GENERATED_AT),
            schema_version=_int_header(headers, storage_snapshot.HEADER_SCHEMA_VERSION),
            service_version=headers.get(
                storage_snapshot.HEADER_SERVICE_VERSION.lower(), ""
            ),
            sha256=_header(headers, storage_snapshot.HEADER_SHA256),
            notes=_int_header(headers, storage_snapshot.HEADER_NOTES),
        )
    finally:
        # install 成功時暫存檔已被 rename 走；其餘情況刪掉半檔
        partial.unlink(missing_ok=True)
