"""`python -m lore_vault.doctor [--json] [--category NAME ...] [--db PATH]
[--embedding-dim N]` 的進入點。

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
