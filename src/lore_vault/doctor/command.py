"""`python -m lore_vault.doctor [--json] [--category NAME ...] [--db PATH]
[--embedding-dim N] [--backup-dir DIR] [--backup-max-age-hours H]
[--snapshot-dir DIR] [--snapshot-max-age-hours H] [--spool-dir DIR] [--client-env FILE]
[--spool-warn-age-hours H] [--spool-fail-age-hours H] [--concept-snapshot FILE]
[--concept-snapshot-max-age-hours H] [--mcp-concept-snapshot-path FILE] [--config FILE]`
的進入點。

`--db` 以唯讀開啟（不建檔、不遷移），放進 context 資源 `"db"`；
沒給時 storage 類檢查記為 skipped。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from lore_vault.storage.db import connect_readonly
from lore_vault.storage.errors import StorageError

from .builtin import default_registry
from .framework import DoctorContext, DoctorReport, Registry, Status

_LABELS = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.SKIPPED: "SKIP",
}


def format_text(report: DoctorReport) -> str:
    if not report.outcomes:
        return "沒有註冊任何檢查項。\n"
    lines: list[str] = []
    for outcome in report.outcomes:
        result = outcome.result
        head = f"[{_LABELS[result.status]}] {outcome.name}"
        lines.append(f"{head}  {result.summary}" if result.summary else head)
        if result.counts:
            counts = ", ".join(f"{k}={v}" for k, v in result.counts.items())
            lines.append(f"    計數：{counts}")
        lines.extend(f"    {d}" for d in result.details)
    data = report.to_dict()["summary"]
    lines.append(
        f"共 {data['total']} 項：pass {data['pass']}、fail {data['fail']}、"
        f"warn {data['warn']}、skipped {data['skipped']}"
    )
    return "\n".join(lines) + "\n"


def main(
    argv: Sequence[str] | None = None,
    *,
    registry: Registry | None = None,
    ctx: DoctorContext | None = None,
    stdout: TextIO | None = None,
) -> int:
    """執行 doctor 並輸出報告，回傳 exit code。`registry`／`ctx` 供測試注入。"""
    parser = argparse.ArgumentParser(prog="python -m lore_vault.doctor")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 報告")
    parser.add_argument(
        "--category", action="append", help="只跑指定分類（可重複）", default=None
    )
    parser.add_argument("--db", help="資料庫路徑（唯讀開啟，供 storage 對帳）")
    parser.add_argument(
        "--embedding-dim", type=int, default=None, help="向量維度（對帳用）"
    )
    parser.add_argument("--backup-dir", help="備份目錄（backup.recent 對帳用）")
    parser.add_argument(
        "--backup-max-age-hours",
        type=float,
        default=None,
        help="最近一次備份的門檻（小時，預設 26）",
    )
    parser.add_argument(
        "--snapshot-dir", help="MCP 殼的本地快照目錄（snapshot 對帳用）"
    )
    parser.add_argument(
        "--snapshot-max-age-hours",
        type=float,
        default=None,
        help="快照年齡門檻（小時，預設 24）",
    )
    parser.add_argument(
        "--spool-dir", help="hook 端 episode spool 目錄（spool 對帳用）"
    )
    parser.add_argument(
        "--client-env", help="hook 端設定檔（預設為 spool 目錄上一層的 client.env）"
    )
    parser.add_argument(
        "--spool-warn-age-hours",
        type=float,
        default=None,
        help="最舊一筆待推送的 warn 門檻（小時，預設 1）",
    )
    parser.add_argument(
        "--spool-fail-age-hours",
        type=float,
        default=None,
        help="最舊一筆待推送的 fail 門檻（小時，預設 24）",
    )
    parser.add_argument(
        "--concept-snapshot", help="PreToolUse 讀的 concept 快照檔（對帳用）"
    )
    parser.add_argument(
        "--concept-snapshot-max-age-hours",
        type=float,
        default=None,
        help="concept 快照年齡門檻（小時，預設 24）",
    )
    parser.add_argument(
        "--mcp-concept-snapshot-path",
        help="MCP 殼寫入的 concept 快照（未給則由設定推導；路徑一致性對帳用）",
    )
    parser.add_argument(
        "--config", help="Lore Vault 設定檔（未給則依 LORE_VAULT_CONFIG）"
    )
    args = parser.parse_args(argv)

    out = stdout if stdout is not None else sys.stdout
    registry = registry if registry is not None else default_registry()
    if args.category:
        # 打錯分類名會篩出 0 項而「通過」，直接當參數錯誤
        known = {c.category for c in registry.checks}
        unknown = sorted(set(args.category) - known)
        if unknown:
            parser.error(f"未知分類 {unknown}；可用：{sorted(known)}")
    db = None
    if ctx is None:
        settings: dict[str, object] = {}
        resources: dict[str, object] = {}
        if args.embedding_dim is not None:
            settings["embedding_dim"] = args.embedding_dim
        if args.backup_dir:
            settings["backup_dir"] = args.backup_dir
        if args.backup_max_age_hours is not None:
            settings["backup_max_age_hours"] = args.backup_max_age_hours
        if args.snapshot_dir:
            settings["snapshot_dir"] = args.snapshot_dir
        if args.snapshot_max_age_hours is not None:
            settings["snapshot_max_age_hours"] = args.snapshot_max_age_hours
        optional = {
            "spool_dir": args.spool_dir,
            "client_env": args.client_env,
            "spool_warn_age_hours": args.spool_warn_age_hours,
            "spool_fail_age_hours": args.spool_fail_age_hours,
            "concept_snapshot": args.concept_snapshot,
            "concept_snapshot_max_age_hours": args.concept_snapshot_max_age_hours,
            "mcp_concept_snapshot_path": args.mcp_concept_snapshot_path,
            "config": args.config,
        }
        settings.update({k: v for k, v in optional.items() if v is not None})
        if args.db:
            try:
                db = connect_readonly(args.db)
            except (StorageError, OSError) as exc:
                parser.error(f"無法開啟資料庫：{exc}")
            resources["db"] = db
        ctx = DoctorContext(settings=settings, resources=resources)
    try:
        report = registry.run(ctx, categories=args.category)
    finally:
        if db is not None:
            db.close()
    if args.json:
        # ensure_ascii：不受主控台編碼影響，機器讀取不失真
        out.write(json.dumps(report.to_dict(), ensure_ascii=True, indent=2) + "\n")
    else:
        text = format_text(report)
        encoding = getattr(out, "encoding", None) or "utf-8"
        out.write(text.encode(encoding, errors="replace").decode(encoding))
    return report.exit_code
