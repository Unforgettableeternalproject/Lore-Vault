"""`python -m lore_vault.tasks <子指令>` 的進入點。

exit code：成功 0；驗證失敗／拒絕執行 1；參數錯誤 2（argparse）。

快照推送（D15 UI 已裁決）：`propose`／`validate`／`archive`／`list` 結束後把任務
快照推到服務端側載（`snapshot.push`），失敗只在 stderr 印警告、不改 exit code
（服務不可達不阻塞）；`propose`／`archive` 只在成功時推送（被拒絕的 archive
一次都不碰服務）。`sync` 只做推送，失敗為 1。stdout 不受影響（`list --json`
仍可直接解析）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from lore_vault.doctor.framework import DoctorContext

from . import snapshot
from .archive import ArchiveError, archive_change, describe_result
from .vault_client import ServiceError, VaultClient, load_settings
from .workspace import (
    CONFIG_FILE,
    DEFAULT_DIR,
    META_FILE,
    NAME_RE,
    Workspace,
    default_meta,
    derive_status,
    load_workspace,
    record_base,
    resolve_root,
    validate_change,
    write_yaml,
)

EXIT_OK = 0
EXIT_FAIL = 1

_CONFIG_TEMPLATE = """schema: spec-driven

# 任務層設定（lore_vault.tasks）
# DECISIONS.md 位置（相對於本目錄的上一層）；blocked_by 的 Dn 依其 `### Dn` 小節判定
# decisions_file: docs/DECISIONS.md
"""

_PROPOSAL_TEMPLATE = """# Proposal

{source_line}## Why

（為什麼要做）

## What Changes

-

## Impact

-
"""

_TASKS_TEMPLATE = """# Tasks

## 1. 實作

- [ ] 1.1

## 2. 驗收

- [ ] 2.1
"""


def _split(values: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return list(dict.fromkeys(out))


def _add_client_env(p: argparse.ArgumentParser) -> None:
    p.add_argument("--client-env", help="客戶端設定檔（預設 ~/.lore-vault/client.env）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.tasks")
    parser.add_argument("--root", help=f"任務目錄（預設 ./{DEFAULT_DIR}）")
    parser.add_argument("--decisions", help="DECISIONS.md 路徑（覆寫 config.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建立任務目錄骨架（重複執行不覆寫）")

    p = sub.add_parser("propose", help="建立 change 骨架")
    p.add_argument("name")
    p.add_argument("--source", help="對應 TASKS.md 卡號，例如 T-32")
    p.add_argument(
        "--blocked-by", action="append", help="阻塞的 D 編號（可重複或逗號分隔）"
    )
    p.add_argument("--depends-on", action="append", help="依賴的 change 名稱")
    p.add_argument("--requires-authorization", action="store_true")
    p.add_argument("--skip-specs", action="store_true", help="無規格的純任務")
    p.add_argument("--goal", help="一句話目標（寫入 .openspec.yaml 的 goal）")
    _add_client_env(p)

    p = sub.add_parser("list", help="列出 change 與推導狀態")
    p.add_argument("--json", action="store_true")
    _add_client_env(p)

    p = sub.add_parser("validate", help="檢查格式、requirement 重疊與 base")
    p.add_argument("name", nargs="?")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--record-base", action="store_true", help="補記尚未記錄的 base（不覆寫既有）"
    )
    group.add_argument(
        "--rebase", action="store_true", help="delta 已依主 spec 現值改好後，重記 base"
    )
    _add_client_env(p)

    p = sub.add_parser("archive", help="寫 note、併主 spec、搬到 archive/")
    p.add_argument("name")
    p.add_argument("--authorized-by", help="requires_authorization 的 change 必填")
    p.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="tasks.md 尚有未勾選項目時仍封存（未完成數記進 .openspec.yaml）",
    )
    p.add_argument(
        "--vault", help="Lore Vault vault key（預設由專案目錄 binding 推算）"
    )
    _add_client_env(p)
    p.add_argument("--author", default="lore-vault-tasks", help="寫入 note 的作者名")

    p = sub.add_parser("sync", help="把任務快照推到 Lore Vault（UI 任務畫面）")
    p.add_argument(
        "--vault", help="Lore Vault vault key（預設由專案目錄 binding 推算）"
    )
    _add_client_env(p)

    p = sub.add_parser("doctor", help="任務層對帳")
    p.add_argument("--json", action="store_true")
    _add_client_env(p)
    p.add_argument(
        "--vault",
        help="tasks.snapshot_sync 比對的 vault（預設由專案目錄 binding 推算）",
    )
    p.add_argument(
        "--offline", action="store_true", help="不連服務（相關檢查 skipped）"
    )
    return parser


class _Env:
    def __init__(
        self,
        stdout: TextIO,
        environ: Mapping[str, str] | None,
        cwd: Path,
        stderr: TextIO | None = None,
    ):
        self.out = stdout
        self.err = stderr or sys.stderr
        self.environ = environ
        self.cwd = cwd

    def print(self, *lines: str) -> None:
        for line in lines:
            self.out.write(line + "\n")

    def warn(self, *lines: str) -> None:
        for line in lines:
            self.err.write(line + "\n")


def _workspace(args: argparse.Namespace, env: _Env) -> Workspace | None:
    root = resolve_root(args.root, env.environ, env.cwd)
    if root is None or not root.is_dir():
        env.print(f"找不到任務目錄（{args.root or './' + DEFAULT_DIR}）；先執行 init")
        return None
    return load_workspace(root, args.decisions, env.environ)


def _init(args: argparse.Namespace, env: _Env) -> int:
    root = resolve_root(args.root, env.environ, env.cwd) or (env.cwd / DEFAULT_DIR)
    created = []
    for directory in (root / "specs", root / "changes" / "archive"):
        directory.mkdir(parents=True, exist_ok=True)
    for path, text in (
        (root / CONFIG_FILE, _CONFIG_TEMPLATE),
        (root / "specs" / ".gitkeep", ""),
        (root / "changes" / "archive" / ".gitkeep", ""),
    ):
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            created.append(str(path.relative_to(root)))
    env.print(f"任務目錄：{root}", "新建：" + ("、".join(created) or "無（皆已存在）"))
    return EXIT_OK


def _propose(args: argparse.Namespace, env: _Env, today: str) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    name = args.name
    if not NAME_RE.match(name) or name == "archive":
        env.print(f"change 名稱 {name!r} 只能用小寫英數與 -（kebab-case）")
        return EXIT_FAIL
    path = ws.changes_dir / name
    if path.exists() or any(c.name == name for c in ws.archived()):
        env.print(f"change {name} 已存在")
        return EXIT_FAIL
    blocked_by = _split(args.blocked_by)
    bad = [d for d in blocked_by if not d[:1] == "D" or not d[1:].isdigit()]
    if bad:
        env.print(f"--blocked-by 必須是 D 編號：{', '.join(bad)}")
        return EXIT_FAIL
    meta = default_meta(
        today,
        source=args.source,
        blocked_by=blocked_by,
        depends_on=_split(args.depends_on),
        requires_authorization=args.requires_authorization,
        skip_specs=args.skip_specs,
    )
    if args.goal:
        meta = {
            "schema": meta.pop("schema"),
            "created": meta.pop("created"),
            "goal": args.goal,
            **meta,
        }
    path.mkdir(parents=True)
    write_yaml(path / META_FILE, meta)
    source_line = f"> 來源：{args.source}\n\n" if args.source else ""
    (path / "proposal.md").write_text(
        _PROPOSAL_TEMPLATE.format(source_line=source_line), encoding="utf-8"
    )
    (path / "tasks.md").write_text(_TASKS_TEMPLATE, encoding="utf-8")
    if args.skip_specs:
        hint = (
            "無規格的純任務：編輯 proposal.md 與 tasks.md，"
            f"完成並勾完 tasks.md 後執行 archive {name}"
        )
    else:
        hint = (
            "新增 spec delta（specs/<capability>/spec.md）後執行 "
            f"validate {name} --record-base 記錄 base"
        )
    env.print(f"已建立 {path}", hint)
    return EXIT_OK


def _list(args: argparse.Namespace, env: _Env) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    rows: list[dict[str, Any]] = []
    for change in ws.active() + ws.archived():
        status, reasons = derive_status(change, ws)
        done, total = change.tasks_progress()
        rows.append(
            {
                "change": change.name,
                "status": status,
                "reasons": reasons,
                "blocked_by": list(change.meta.get("blocked_by") or []),
                "depends_on": list(change.meta.get("depends_on") or []),
                "requires_authorization": bool(
                    change.meta.get("requires_authorization")
                ),
                "tasks": f"{done}/{total}",
            }
        )
    if args.json:
        env.out.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        return EXIT_OK
    if not rows:
        env.print("沒有任何 change")
        return EXIT_OK
    header = ["change", "狀態", "blocked_by", "depends_on", "需授權", "tasks"]
    table = [
        [
            r["change"],
            r["status"],
            ",".join(r["blocked_by"]) or "-",
            ",".join(r["depends_on"]) or "-",
            "是" if r["requires_authorization"] else "-",
            r["tasks"],
        ]
        for r in rows
    ]
    widths = [
        max(_width(str(row[i])) for row in [header, *table]) for i in range(len(header))
    ]
    for row in [header, *table]:
        env.print(" | ".join(_pad(str(c), w) for c, w in zip(row, widths, strict=True)))
    for r in rows:
        if r["reasons"]:
            env.print(f"  {r['change']}：" + "；".join(r["reasons"]))
    return EXIT_OK


def _width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _validate(args: argparse.Namespace, env: _Env) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    active = ws.active()
    if args.name:
        targets = [c for c in active if c.name == args.name]
        if not targets:
            env.print(f"找不到 active change：{args.name}")
            return EXIT_FAIL
    else:
        targets = active
    failed = False
    for change in targets:
        if (args.record_base or args.rebase) and not change.meta_error:
            changed = record_base(change, ws, overwrite=args.rebase)
            if changed:
                env.print(f"{change.name}：已記錄 base " + "、".join(changed))
        errors = validate_change(change, ws, active)
        if errors:
            failed = True
            env.print(f"[FAIL] {change.name}", *(f"    {e}" for e in errors))
        else:
            env.print(f"[ OK ] {change.name}")
    return EXIT_FAIL if failed else EXIT_OK


def _client_factory(args: argparse.Namespace, env: _Env) -> Callable[[], VaultClient]:
    return lambda: VaultClient(
        load_settings(getattr(args, "client_env", None), env.environ)
    )


_PUSH_ERRORS = (ServiceError, snapshot.SnapshotTooLarge, OSError, ValueError)


def _push(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
    *,
    vault: str | None = None,
) -> snapshot.PushResult:
    root = resolve_root(args.root, env.environ, env.cwd)
    if root is None or not root.is_dir():
        raise ServiceError("找不到任務目錄")
    ws = load_workspace(root, args.decisions, env.environ)
    client = (client_factory or _client_factory(args, env))()
    if not client.settings.push_configured:
        raise ServiceError(client.describe())
    return snapshot.push(ws, client, vault=vault)


def _detail(exc: BaseException) -> str:
    return exc.detail if isinstance(exc, ServiceError) else str(exc)


def _auto_sync(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
) -> None:
    """子指令結尾的快照推送：任何失敗只在 stderr 警告，不影響 exit code。"""
    try:
        _push(args, env, client_factory, vault=getattr(args, "vault", None))
    except _PUSH_ERRORS as exc:
        env.warn(
            f"警告：任務快照未同步到 Lore Vault（{_detail(exc)}）；"
            "本機指令已完成，稍後可執行 sync 重推"
        )


def _sync(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
) -> int:
    if _workspace(args, env) is None:
        return EXIT_FAIL
    try:
        result = _push(args, env, client_factory, vault=args.vault)
    except _PUSH_ERRORS as exc:
        env.print(f"sync 失敗：{_detail(exc)}")
        return EXIT_FAIL
    env.print(
        f"已同步 {result.changes} 個 change 到 {result.vault}"
        f"（{result.size_bytes} 位元組，{result.updated}）"
    )
    return EXIT_OK


def _archive(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
    now: _dt.datetime | None,
) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    try:
        result = archive_change(
            ws,
            args.name,
            client_factory=client_factory or _client_factory(args, env),
            authorized_by=args.authorized_by,
            allow_incomplete=args.allow_incomplete,
            vault=args.vault,
            author=args.author,
            now=now,
        )
    except ArchiveError as exc:
        env.print(f"archive 中止：{exc}", *(f"    {d}" for d in exc.details))
        return EXIT_FAIL
    env.print(*describe_result(result))
    return EXIT_OK


def _doctor(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
) -> int:
    from lore_vault.doctor.command import format_text

    from . import checks

    root = resolve_root(args.root, env.environ, env.cwd)
    settings: dict[str, Any] = {}
    resources: dict[str, Any] = {}
    if root is not None and root.is_dir():
        ws = load_workspace(root, args.decisions, env.environ)
        settings = {"tasks_root": str(ws.root), "decisions_path": ws.decisions_path}
        if args.vault:
            settings["vault"] = args.vault
        if not args.offline:
            client = (client_factory or _client_factory(args, env))()
            # 未設定推送（沒有 client.env）時要對服務的檢查記為 skipped
            if client.settings.push_configured:
                resources["client"] = client
    report = checks.default_registry().run(DoctorContext(settings, resources))
    if args.json:
        env.out.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    else:
        env.out.write(format_text(report))
    return report.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    client_factory: Callable[[], VaultClient] | None = None,
    now: _dt.datetime | None = None,
    stderr: TextIO | None = None,
) -> int:
    """`environ`／`cwd`／`client_factory`／`now`／`stderr` 供測試注入。"""
    args = _parser().parse_args(argv)
    out = stdout or sys.stdout
    env = _Env(out, environ, cwd or Path.cwd(), stderr)
    today = (now or _dt.datetime.now()).date().isoformat()
    if args.command == "init":
        return _init(args, env)
    if args.command == "sync":
        return _sync(args, env, client_factory)
    if args.command == "doctor":
        return _doctor(args, env, client_factory)
    if args.command == "propose":
        code = _propose(args, env, today)
    elif args.command == "list":
        code = _list(args, env)
    elif args.command == "validate":
        code = _validate(args, env)
    else:
        code = _archive(args, env, client_factory, now)
    if _should_sync(args, env, code):
        _auto_sync(args, env, client_factory)
    return code


def _should_sync(args: argparse.Namespace, env: _Env, code: int) -> bool:
    root = resolve_root(args.root, env.environ, env.cwd)
    if root is None or not root.is_dir():
        return False
    # propose／archive 被拒絕時不碰服務；list／validate 的結果不改變快照內容
    return code == EXIT_OK or args.command in ("list", "validate")
