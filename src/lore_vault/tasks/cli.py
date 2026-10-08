"""`python -m lore_vault.tasks <子指令>` 的進入點。

exit code：成功 0；驗證失敗／拒絕執行 1；參數錯誤 2（argparse）。

快照推送（D15 UI 已裁決）：`propose`／`validate`／`archive`／`list` 結束後把任務
快照推到服務端側載（`snapshot.push`），失敗只在 stderr 印警告、不改 exit code
（服務不可達不阻塞）；`propose`／`archive` 只在成功時推送（被拒絕的 archive
一次都不碰服務）。`sync` 只做推送，失敗為 1。stdout 不受影響（`list --json`
仍可直接解析）。

快照來源（MCP-T6）：vault 已有服務端 `task-index` 時一律以服務端內容計算
（與 MCP 同一套，`snapshot.push_remote`），否則照舊以本機目錄計算。

服務端同步模式（`config.yaml` 標 `remote: true`，由 `init --remote` 或 MCP stdio
`init` 寫入）：propose／list／validate／archive 改以服務端版本化內容為準、本機
`changes/` 為工作副本，另有 push／pull／sync-specs 子指令，細節見 `cli_remote`。
`--offline`、沒有標記或沒有客戶端設定時維持純本機模式（行為與先前一致）；
本機模式的 archive 拒絕已同步到服務端（有 `remote_version`）的 change，避免與服務端
的封存重複寫 note。
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

from . import cli_remote, migrate, remote_ops, snapshot
from . import remote_store as rs
from .archive import ArchiveError, archive_change, describe_result
from .vault_client import (
    ServiceError,
    ServiceRejected,
    ServiceUnavailable,
    VaultClient,
    load_settings,
)
from .workspace import (
    CONFIG_FILE,
    DEFAULT_DIR,
    META_FILE,
    NAME_RE,
    Workspace,
    default_meta,
    derive_status,
    enable_remote,
    load_workspace,
    record_base,
    remote_enabled,
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


def _add_offline(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--offline",
        action="store_true",
        help="不連服務：純本機模式（不同步服務端內容、不推任務快照）",
    )


def _add_vault(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--vault", help="Lore Vault vault key（預設由專案目錄 binding 推算）"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.tasks")
    parser.add_argument("--root", help=f"任務目錄（預設 ./{DEFAULT_DIR}）")
    parser.add_argument("--decisions", help="DECISIONS.md 路徑（覆寫 config.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="建立任務目錄骨架（重複執行不覆寫）")
    p.add_argument(
        "--remote",
        action="store_true",
        help="啟用服務端同步：建立服務端任務層、推主 spec 與 DECISIONS 鏡像，"
        "並在 config.yaml 標記 remote: true",
    )
    _add_vault(p)
    _add_client_env(p)
    _add_offline(p)

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
    _add_offline(p)

    p = sub.add_parser("list", help="列出 change 與推導狀態")
    p.add_argument("--json", action="store_true")
    _add_client_env(p)
    _add_offline(p)

    p = sub.add_parser(
        "disable",
        help="停用服務端任務層（內容保留；init --remote 或 UI 重新啟用）",
    )
    _add_vault(p)
    p.add_argument("--by", default="CLI", help="記進停用資訊的操作者名稱")
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
    _add_offline(p)

    p = sub.add_parser("archive", help="寫 note、併主 spec、搬到 archive/")
    p.add_argument("name")
    p.add_argument(
        "--authorized-by",
        help="純本機模式（--offline）下 requires_authorization 的 change 必填；"
        "服務端同步模式不接受，改在 UI 任務頁核准",
    )
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
    _add_offline(p)

    p = sub.add_parser("sync", help="把任務快照推到 Lore Vault（UI 任務畫面）")
    p.add_argument(
        "--vault", help="Lore Vault vault key（預設由專案目錄 binding 推算）"
    )
    _add_client_env(p)

    p = sub.add_parser(
        "push", help="服務端同步模式：把本機工作副本的修改推上服務端（版本 CAS）"
    )
    p.add_argument("name")
    _add_vault(p)
    _add_client_env(p)

    p = sub.add_parser("pull", help="服務端同步模式：以服務端內容更新本機工作副本")
    p.add_argument("name")
    p.add_argument("--overwrite", action="store_true", help="捨棄本機未推送的修改")
    _add_vault(p)
    _add_client_env(p)

    p = sub.add_parser(
        "sync-specs",
        help="服務端同步模式：已封存待落地的 change 寫回本機 specs/ 並搬進 archive/",
    )
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="本機工作副本有未推送的修改時仍落地（捨棄那些修改）",
    )
    _add_vault(p)
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

    migrate.add_parser(sub)
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


def init_root(root: Path) -> list[str]:
    """建立任務目錄骨架（重複執行不覆寫）；回傳新建的檔案（相對於 root）。
    CLI `init` 與 MCP `tasks(action="init")` 的 stdio 本機部分共用。"""
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
            created.append(path.relative_to(root).as_posix())
    return created


def _init(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
) -> int:
    root = resolve_root(args.root, env.environ, env.cwd) or (env.cwd / DEFAULT_DIR)
    created = [str(Path(c)) for c in init_root(root)]
    env.print(f"任務目錄：{root}", "新建：" + ("、".join(created) or "無（皆已存在）"))
    # 服務端部分：明確 --remote，或已標記 remote 的專案重跑 init（推鏡像與 DECISIONS）
    if args.offline or not (args.remote or remote_enabled(root)):
        return EXIT_OK
    if enable_remote(root):
        env.print("config.yaml 已標記 remote: true（change 以服務端為準）")
    ws = load_workspace(root, args.decisions, env.environ)
    client = cli_remote.client_for(
        args, env, ws, client_factory or _client_factory(args, env)
    )
    if client is None:
        env.print("服務端初始化未執行：未設定服務位址與 token")
        return EXIT_FAIL
    return cli_remote.init_remote(env, client, ws, args.vault)


def _propose(
    args: argparse.Namespace,
    env: _Env,
    today: str,
    client_factory: Callable[[], VaultClient] | None = None,
) -> int:
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
    source_line = f"> 來源：{args.source}\n\n" if args.source else ""
    proposal = _PROPOSAL_TEMPLATE.format(source_line=source_line)
    hint = _propose_hint(args, name)
    client = cli_remote.client_for(
        args, env, ws, client_factory or _client_factory(args, env)
    )
    if client is not None:
        code = cli_remote.propose(
            env, client, ws, name, meta, proposal, _TASKS_TEMPLATE, hint
        )
        if code is not None:
            return code
    path.mkdir(parents=True)
    write_yaml(path / META_FILE, meta)
    (path / "proposal.md").write_text(proposal, encoding="utf-8")
    (path / "tasks.md").write_text(_TASKS_TEMPLATE, encoding="utf-8")
    env.print(f"已建立 {path}", hint)
    return EXIT_OK


def _propose_hint(args: argparse.Namespace, name: str) -> str:
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
    return hint


def _list(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None = None,
) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    client = cli_remote.client_for(
        args, env, ws, client_factory or _client_factory(args, env)
    )
    remote_rows = cli_remote.list_rows(env, client, ws) if client else None
    if not args.json:
        # stdout 的 --json 格式（陣列）不變；狀態只在文字模式顯示
        _print_layer_state(args, env, ws, client, client_factory)
    rows: list[dict[str, Any]] = list(remote_rows or [])
    for change in [] if remote_rows is not None else ws.active() + ws.archived():
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


def _print_layer_state(
    args: argparse.Namespace,
    env: _Env,
    ws: Workspace,
    client: VaultClient | None,
    client_factory: Callable[[], VaultClient] | None,
) -> None:
    """服務端任務層是否啟用（同 MCP list 的 enabled／local_initialized）。"""
    local = remote_enabled(ws.root)
    state = None
    if client is None and not args.offline:
        candidate = (client_factory or _client_factory(args, env))()
        client = candidate if candidate.settings.push_configured else None
    if client is not None and not args.offline:
        state = cli_remote.index_state(env, client, ws)
    if state is None:
        remote = "無法確認（離線或未設定服務）"
    elif state.disabled is not None:
        remote = f"已停用（{state.describe()}，內容保留）"
    else:
        remote = "已啟用" if state.exists else "未啟用"
    env.print(
        f"任務層：服務端{remote}；本機"
        + ("已初始化（remote: true）" if local else "未標記 remote: true")
    )
    if state is not None and state.enabled and not local:
        env.print("  服務端已啟用任務層：執行 init --remote 建立本機工作副本（冪等）")


def _disable(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
    now: _dt.datetime | None,
) -> int:
    root = resolve_root(args.root, env.environ, env.cwd)
    ws = load_workspace(root, args.decisions, env.environ) if root else None
    if ws is None and not args.vault:
        env.print("disable 失敗：找不到任務目錄；帶 --vault 指定 vault")
        return EXIT_FAIL
    client = (client_factory or _client_factory(args, env))()
    if not client.settings.push_configured:
        env.print(f"disable 失敗：{client.describe()}")
        return EXIT_FAIL
    return cli_remote.disable(env, client, ws, args.vault, by=args.by, now=now)


def _width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _validate(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None = None,
) -> int:
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    client = cli_remote.client_for(
        args, env, ws, client_factory or _client_factory(args, env)
    )
    if client is not None:
        code = cli_remote.validate(args, env, client, ws)
        if code is not None:
            return code
        ws = load_workspace(ws.root, args.decisions, env.environ)
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
    decisions: bool = False,
) -> list[snapshot.PushResult]:
    """vault 已有服務端 `task-index` → 以服務端內容計算（與 MCP 同一套）；
    否則照舊以本機目錄計算。`decisions`：同時推 DECISIONS 鏡像（sync）。"""
    root = resolve_root(args.root, env.environ, env.cwd)
    if root is None or not root.is_dir():
        raise ServiceError("找不到任務目錄")
    ws = load_workspace(root, args.decisions, env.environ)
    client = (client_factory or _client_factory(args, env))()
    if not client.settings.push_configured:
        raise ServiceError(client.describe())

    async def remote() -> list[snapshot.PushResult] | None:
        store = await cli_remote.open_store(client, ws, vault, writable=False)
        state = await store.index_state()
        if not state.exists:
            return None
        if state.disabled is not None:
            raise rs.StoreError(
                "tasks_disabled",
                f"{store.vault} 的任務層已停用（{state.describe()}），不推送快照",
            )
        if decisions:
            await remote_ops.push_decisions(store, ws)
        return [await snapshot.push_remote(store, ws)]

    try:
        results = cli_remote.run(remote)
    except cli_remote.Offline as exc:
        raise ServiceUnavailable(str(exc)) from None
    except rs.RemoteError as exc:
        raise ServiceRejected(exc.message, exc.status, exc.body) from None
    except rs.StoreError as exc:
        raise ServiceRejected(exc.message) from None
    if results is not None:
        return results
    return snapshot.push(ws, client, vault=vault)


def _detail(exc: BaseException) -> str:
    return exc.detail if isinstance(exc, ServiceError) else str(exc)


def _auto_sync(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
    *,
    vault: str | None = None,
) -> None:
    """子指令結尾的快照推送：任何失敗只在 stderr 警告，不影響 exit code。

    `vault`：沒記 vault 的 change 歸屬的預設 vault；None 由專案目錄 binding 推算。
    archive 寫入的 vault 已記在該 change 的 metadata，推送依 metadata 分份涵蓋。"""
    try:
        _push(args, env, client_factory, vault=vault)
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
        results = _push(args, env, client_factory, vault=args.vault, decisions=True)
    except _PUSH_ERRORS as exc:
        env.print(f"sync 失敗：{_detail(exc)}")
        return EXIT_FAIL
    for result in results:
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
    factory = client_factory or _client_factory(args, env)
    client = cli_remote.client_for(args, env, ws, factory)
    if client is not None:
        return cli_remote.archive(args, env, client, ws, now)
    local = ws.find_active(args.name)
    if local is not None and rs.REMOTE_VERSION_KEY in local.meta:
        # 已同步到服務端的 change 由服務端兩段式封存；本機模式再寫一次 note
        # 會與 MCP／其他機器的封存重複（在任何網路呼叫之前拒絕）
        env.print(
            f"archive 中止：{args.name} 已同步到服務端（remote_version "
            f"{local.meta[rs.REMOTE_VERSION_KEY]}），不能以本機模式封存",
            "    連線後不帶 --offline 重跑（config.yaml 須標記 remote: true）",
        )
        return EXIT_FAIL
    try:
        result = archive_change(
            ws,
            args.name,
            client_factory=factory,
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
        return _init(args, env, client_factory)
    if args.command == "sync":
        return _sync(args, env, client_factory)
    if args.command == "doctor":
        return _doctor(args, env, client_factory)
    if args.command == "migrate":
        return migrate.run(args, env, client_factory)
    if args.command == "disable":
        return _disable(args, env, client_factory, now)
    if args.command == "propose":
        code = _propose(args, env, today, client_factory)
    elif args.command == "list":
        code = _list(args, env, client_factory)
    elif args.command == "validate":
        code = _validate(args, env, client_factory)
    elif args.command in ("push", "pull", "sync-specs"):
        code = _remote_only(args, env, client_factory, now)
    else:
        # archive 寫入的 vault 已記進該 change 的 metadata：推送依 metadata 分份，
        # 原本收它的預設 vault 也會重推、把它移除
        code = _archive(args, env, client_factory, now)
    if _should_sync(args, env, code):
        _auto_sync(args, env, client_factory)
    return code


def _remote_only(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
    now: _dt.datetime | None,
) -> int:
    """push／pull／sync-specs：只在服務端同步模式有意義。"""
    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    if not remote_enabled(ws.root):
        env.print(
            f"{args.command} 需要服務端同步模式：先執行 init --remote"
            "（config.yaml 標記 remote: true）"
        )
        return EXIT_FAIL
    client = cli_remote.client_for(
        args, env, ws, client_factory or _client_factory(args, env)
    )
    if client is None:
        env.print(f"{args.command} 失敗：未設定服務位址與 token")
        return EXIT_FAIL
    if args.command == "push":
        return cli_remote.push(args, env, client, ws)
    if args.command == "pull":
        return cli_remote.pull(args, env, client, ws)
    return cli_remote.sync_specs(args, env, client, ws, now)


def _should_sync(args: argparse.Namespace, env: _Env, code: int) -> bool:
    if getattr(args, "offline", False):
        return False
    root = resolve_root(args.root, env.environ, env.cwd)
    if root is None or not root.is_dir():
        return False
    # propose／archive 被拒絕時不碰服務；list／validate 的結果不改變快照內容
    return code == EXIT_OK or args.command in ("list", "validate")
