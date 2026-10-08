"""CLI 的服務端同步模式（TASK_LAYER_MCP §1.1～1.4、MCP-T6）。

啟用條件（`client_for`）：任務目錄 `config.yaml` 標 `remote: true`
（stdio `init` 寫入）、沒帶 `--offline`、且客戶端設定了服務位址與 token。
不符合時呼叫端照舊走純本機流程。

服務端是 change 工作內容的權威，本機 `changes/<name>/` 是工作副本：

- 動作前先對齊（`align`）：本機沒有或落後 → 以服務端內容寫回；本機有未推送的修改
  → 以 `.openspec.yaml` 的 `remote_version` 做 CAS 推送；服務端在那之後被改過
  （`version_conflict`）或兩邊都改過（`diverged`）→ 拒絕並提示 `pull` 或手動合併，
  一律不靜默覆蓋
- `archive`：服務端兩段式一次做完——段一（寫 note、推進鏡像、`pending_apply`）與
  MCP 共用 `remote_ops`，note 的 write-ahead 記錄存在同一份版本化 change 文件，
  CLI 與 MCP 對同一個 change 不會重複寫 note；段二（`sync_specs`）接著在本機落地。
  `requires_authorization` 的 change 與 MCP 一樣只認 UI 核准紀錄（內容雜湊相符）；
  `--authorized-by` 只在 `--offline`／純本機模式有效，同步模式帶了它直接拒絕
- 服務不可達：propose／list／validate 退回本機模式並在 stderr 警告；
  archive／push／pull／sync-specs 失敗（exit 1）
"""

from __future__ import annotations

import argparse
import datetime as _dt
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anyio

from lore_vault.binding import resolve_binding

from . import remote_ops
from . import remote_store as rs
from .vault_client import VaultClient
from .workspace import (
    STATUS_AUTH,
    STATUS_PENDING_APPLY,
    Workspace,
    derive_status,
    load_workspace,
    record_base,
    remote_enabled,
    validate_change,
)

if TYPE_CHECKING:
    from .cli import _Env

EXIT_OK = 0
EXIT_FAIL = 1

CONFLICT_HINT = (
    "服務端已有較新的版本：先備份本機修改，執行 `pull {name} --overwrite` "
    "取回最新內容，把修改重新套上後再 `push {name}`（或手動合併後再推送）"
)

PENDING_REASON = "執行 sync-specs 落地本機 specs/"
AUTH_REASON = "需艾斯維爾授權（請在 UI 任務頁核准）"
AUTHORIZED_BY_REJECTED = (
    "archive 中止：服務端同步模式不接受 --authorized-by（需授權的 change 一律要在"
    " UI 任務頁核准）"
)
AUTHORIZED_BY_HINT = (
    "    請使用者在 UI 任務頁核准後，不帶 --authorized-by 重跑；"
    "只在本機封存（不進服務端）時才用 --offline --authorized-by"
)


class Offline(Exception):
    """服務不可達：呼叫端決定退回本機模式或失敗。"""


def client_for(
    args: argparse.Namespace,
    env: _Env,
    ws: Workspace,
    factory: Callable[[], VaultClient],
) -> VaultClient | None:
    """服務端同步模式要用的 client；不啟用時回 None（呼叫端走本機流程）。"""
    if getattr(args, "offline", False) or not remote_enabled(ws.root):
        return None
    client = factory()
    if not client.settings.push_configured:
        env.warn(
            f"警告：config.yaml 標記 remote: true，但{client.describe()}；"
            "以本機模式執行"
        )
        return None
    return client


def run(fn: Callable[[], Awaitable[Any]]) -> Any:
    """在 anyio 事件迴圈執行（archive 段一的 note 寫入需要 worker thread 回到迴圈）。
    服務不可達轉成 `Offline`。"""
    try:
        return anyio.run(fn)
    except rs.RemoteUnreachable as exc:
        raise Offline(exc.detail) from None


async def open_store(
    client: VaultClient, ws: Workspace, vault: str | None
) -> rs.RemoteStore:
    post = rs.vault_client_post(client)
    key = vault or resolve_binding(ws.project_root).key
    resolved = await rs.RemoteStore(post, str(key)).resolve_vault(str(key))
    return rs.RemoteStore(post, resolved)


def _fail(env: _Env, prefix: str, exc: Exception) -> int:
    if isinstance(exc, rs.VersionConflict):
        current = "不存在" if exc.current is None else f"v{exc.current.version}"
        env.print(
            f"{prefix}：版本衝突（本機工作副本基於 v{exc.expected}，"
            f"服務端目前 {current}），未覆寫服務端",
            "    " + CONFLICT_HINT.format(name=exc.current.name if exc.current else ""),
        )
        return EXIT_FAIL
    if isinstance(exc, rs.StoreError):
        details = exc.extra.get("details") or []
        lines = [f"{prefix}：{exc.message}", *(f"    {d}" for d in details)]
        hint = exc.extra.get("hint")
        if hint:
            lines.append(f"    {hint}")
        env.print(*lines)
        return EXIT_FAIL
    if isinstance(exc, rs.RemoteError):
        env.print(f"{prefix}：服務拒絕（{exc.status}）：{exc.message}")
        return EXIT_FAIL
    raise exc


# ── 對齊本機工作副本與服務端 ────────────────────────────────────────


async def align(
    store: rs.RemoteStore, ws: Workspace, name: str, *, pull: bool = True
) -> tuple[rs.RemoteChange, str]:
    """讓本機工作副本與服務端一致；回傳 (服務端 change, 做了什麼)。

    做了什麼：`in_sync`、`pulled`（本機沒有或落後，已寫回）、`pushed`（本機修改以
    `remote_version` CAS 推上去）、`created`（服務端沒有、本機從未同步過，建立）、
    `not_active`（服務端已封存，未動本機）。`pull=False`（`push` 子指令）時落後
    不寫回、回 `behind`。衝突一律拋錯，不覆寫任一邊。"""
    remote = await store.get_change(name)
    local = ws.find_active(name)
    if remote is None:
        if local is None:
            raise rs.StoreError("change_not_found", f"本機與服務端都沒有 change {name}")
        if rs.REMOTE_VERSION_KEY in local.meta:
            raise rs.StoreError(
                "remote_missing",
                f"本機 {name} 記錄已同步過 v{local.meta[rs.REMOTE_VERSION_KEY]}，"
                "但服務端已沒有這個 change；請人工確認後再處理",
            )
        created = await store.create_change(rs.local_doc(local))
        rs.write_local(ws, created)
        return created, "created"
    if remote.state != rs.STATE_ACTIVE:
        return remote, "not_active"
    state = rs.local_state(ws, remote)
    if state.state == "in_sync":
        return remote, "in_sync"
    if state.state in ("absent", "behind"):
        if not pull:
            return remote, "behind"
        rs.write_local(ws, remote)
        return remote, "pulled"
    if state.state == "unknown":
        raise rs.StoreError(
            "local_meta_invalid", f"本機 changes/{name}/.openspec.yaml 無法解析"
        )
    if state.state == "diverged":
        raise rs.StoreError(
            "diverged",
            f"{name}：本機有未推送的修改，服務端也已更新到 v{remote.version}"
            f"（本機基於 v{state.local_version}），未覆寫任一邊",
            hint=CONFLICT_HINT.format(name=name),
        )
    # local_modified：以本機記錄的版本做 CAS
    if remote.meta.get("notes") or remote.meta.get("note_digests"):
        raise rs.StoreError(
            "archive_in_progress",
            f"{name} 的 archive 已在服務端開始，內容不可再改",
            hint="重跑 archive 完成封存（本機修改不會推送）",
        )
    base = state.local_version
    if base is None:
        raise rs.StoreError(
            "diverged",
            f"{name}：本機與服務端各有一份從未同步過的內容，未覆寫任一邊",
            hint=CONFLICT_HINT.format(name=name),
        )
    local = ws.find_active(name)
    assert local is not None
    pushed = rs.RemoteChange.from_doc(rs.local_doc(local), base, store.vault)
    await store.save_change(pushed)  # expected_version=base；過期 → VersionConflict
    rs.write_local(ws, pushed)
    return pushed, "pushed"


def _describe_align(name: str, action: str, change: rs.RemoteChange) -> str | None:
    return {
        "pulled": f"{name}：已從服務端取回 v{change.version}",
        "pushed": f"{name}：本機修改已推送到服務端 v{change.version}",
        "created": f"{name}：已在服務端建立 v{change.version}",
    }.get(action)


# ── 子指令 ──────────────────────────────────────────────────────────


def propose(
    env: _Env,
    client: VaultClient,
    ws: Workspace,
    name: str,
    meta: dict[str, Any],
    proposal: str,
    tasks: str,
    hint: str,
) -> int | None:
    """在服務端建立 change 並寫出本機工作副本；服務不可達回 None（呼叫端退回本機）。"""

    async def go() -> rs.RemoteChange:
        store = await open_store(client, ws, None)
        doc = rs.new_doc(name, meta, proposal, tasks)
        return await store.create_change(doc)

    try:
        change = run(go)
    except Offline as exc:
        env.warn(
            f"警告：服務不可達（{exc}），{name} 只建在本機；"
            f"連線後執行 push {name} 推上服務端"
        )
        return None
    except (rs.StoreError, rs.RemoteError) as exc:
        return _fail(env, "propose 失敗", exc)
    path = rs.write_local(ws, change)
    env.print(f"已建立 {path}（服務端 v{change.version}）", hint)
    return EXIT_OK


def list_rows(
    env: _Env, client: VaultClient, ws: Workspace
) -> list[dict[str, Any]] | None:
    """服務端內容推導的狀態表；服務不可達回 None（呼叫端退回本機）。"""

    async def go() -> list[dict[str, Any]]:
        store = await open_store(client, ws, None)
        changes, archived = await store.list_changes(include_archived=True)
        rws = await remote_ops.remote_workspace(store, ws, changes, archived)
        rows = []
        live = [c for c in changes if c.state != rs.STATE_ARCHIVED]
        done = [c for c in changes if c.state == rs.STATE_ARCHIVED]
        for change in live + done:
            status, reasons = derive_status(change, rws)
            if change.state == rs.STATE_PENDING_APPLY:
                status, reasons = STATUS_PENDING_APPLY, [PENDING_REASON]
            elif status == STATUS_AUTH:
                reasons = [AUTH_REASON]
            progress_done, total = change.tasks_progress()
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
                    "tasks": f"{progress_done}/{total}",
                    "state": change.state,
                    "version": change.version,
                }
            )
        return rows

    try:
        return run(go)
    except Offline as exc:
        env.warn(f"警告：服務不可達（{exc}），改列本機工作副本")
        return None


def validate(
    args: argparse.Namespace, env: _Env, client: VaultClient, ws: Workspace
) -> int | None:
    """先推鏡像與 DECISIONS、對齊工作副本，再以本機邏輯驗證；record_base／rebase 改到的
    base 以 CAS 推回服務端。服務不可達回 None（呼叫端退回本機驗證）。"""
    lines: list[str] = []

    async def go() -> int:
        store = await open_store(client, ws, None)
        changes, _ = await store.list_changes()
        await remote_ops.push_mirrors(store, ws, changes)
        await remote_ops.push_decisions(store, ws)
        remote_names = {c.name: c for c in changes}
        local_names = {c.name for c in ws.active()}
        if args.name:
            names = [args.name]
        else:
            names = sorted(
                local_names
                | {n for n, c in remote_names.items() if c.state == rs.STATE_ACTIVE}
            )
        failed = False
        synced: list[str] = []
        for name in names:
            remote = remote_names.get(name)
            if remote is not None and remote.state != rs.STATE_ACTIVE:
                lines.append(
                    f"[SKIP] {name}：已在服務端封存（待落地），執行 sync-specs"
                )
                continue
            if remote is None and name not in local_names:
                lines.append(f"找不到 active change：{name}")
                failed = True
                continue
            try:
                change, action = await align(store, ws, name)
            except rs.StoreError as exc:
                failed = True
                lines.append(f"[FAIL] {name}")
                lines.append(f"    {exc.message}")
                if isinstance(exc, rs.VersionConflict):
                    lines.append("    " + CONFLICT_HINT.format(name=name))
                elif exc.extra.get("hint"):
                    lines.append(f"    {exc.extra['hint']}")
                continue
            note = _describe_align(name, action, change)
            if note:
                lines.append(note)
            synced.append(name)
        local = load_workspace(ws.root, args.decisions, env.environ)
        skip = {n for n, c in remote_names.items() if c.state != rs.STATE_ACTIVE}
        active = [c for c in local.active() if c.name not in skip]
        for change in [c for c in active if c.name in synced]:
            if (args.record_base or args.rebase) and not change.meta_error:
                changed = record_base(change, local, overwrite=args.rebase)
                if changed:
                    lines.append(f"{change.name}：已記錄 base " + "、".join(changed))
                    try:
                        await align(store, local, change.name)
                    except rs.StoreError as exc:
                        failed = True
                        lines.append(f"    base 未推送到服務端：{exc.message}")
            errors = validate_change(change, local, active)
            if errors:
                failed = True
                lines.append(f"[FAIL] {change.name}")
                lines.extend(f"    {e}" for e in errors)
            else:
                lines.append(f"[ OK ] {change.name}")
        return EXIT_FAIL if failed else EXIT_OK

    try:
        code = run(go)
    except Offline as exc:
        env.print(*lines)
        env.warn(f"警告：服務不可達（{exc}），以本機工作副本驗證（未同步）")
        return None
    except rs.RemoteError as exc:
        env.print(*lines)
        return _fail(env, "validate 失敗", exc)
    env.print(*lines)
    return code


def archive(
    args: argparse.Namespace,
    env: _Env,
    client: VaultClient,
    ws: Workspace,
    now: _dt.datetime | None,
) -> int:
    """服務端兩段式：對齊 → 推鏡像 → 段一（note、鏡像推進、pending_apply）→ 段二落地。
    已是 pending_apply 的 change 直接落地（續跑）。"""
    name = args.name
    # 同步模式只認 UI 核准紀錄：帶 --authorized-by 在任何網路呼叫之前拒絕
    if (getattr(args, "authorized_by", None) or "").strip():
        env.print(AUTHORIZED_BY_REJECTED, AUTHORIZED_BY_HINT)
        return EXIT_FAIL
    clock = (lambda: now) if now is not None else (lambda: _dt.datetime.now(_dt.UTC))
    lines: list[str] = []

    async def go() -> dict[str, Any]:
        store = await open_store(client, ws, args.vault)
        change, action = await align(store, ws, name)
        note = _describe_align(name, action, change)
        if note:
            lines.append(note)
        phase1: dict[str, Any] | None = None
        if change.state == rs.STATE_ACTIVE:
            record = await remote_ops.check_authorization(store, change)
            if record is not None:
                lines.append(
                    f"UI 核准：{record.authorized_by}（{record.authorized_at}，"
                    f"v{record.change_version}）"
                )
            changes, _ = await store.list_changes()
            await remote_ops.push_mirrors(store, ws, changes)
            plan, merged, mirrors = await remote_ops.archive_plan(
                store,
                change,
                local=ws,
                authorized_by=record.authorized_by if record else None,
                authorized_version=record.change_version if record else None,
                allow_incomplete=args.allow_incomplete,
                reason=None,
            )
            phase1 = await remote_ops.archive_execute(
                store,
                change,
                merged,
                mirrors,
                authorized_by=record.authorized_by if record else None,
                authorization=record.to_meta() if record else None,
                reason=None,
                author=args.author,
                now=clock(),
            )
        elif change.state == rs.STATE_ARCHIVED:
            raise rs.StoreError("change_not_active", f"{name} 已封存並落地")
        landed = await remote_ops.sync_specs(store, ws, name, now=clock)
        return {"phase1": phase1, "landed": landed["results"], "vault": store.vault}

    try:
        result = run(go)
    except Offline as exc:
        env.print(*lines)
        env.print(
            f"archive 中止：服務不可達（{exc}）；change 以服務端為準，"
            "服務恢復後重跑（已寫入的 note 會沿用、不會重複）"
        )
        return EXIT_FAIL
    except (rs.StoreError, rs.RemoteError) as exc:
        env.print(*lines)
        return _fail(env, "archive 中止", exc)
    env.print(*lines)
    phase1 = result["phase1"]
    landed = result["landed"][0] if result["landed"] else {}
    env.print(
        f"已封存 {name} → {ws.archive_dir / landed.get('archive_dir', '')}",
    )
    if phase1 is not None:
        env.print(f"note_id：{phase1['note_id']}")
        if phase1["written"]:
            env.print("本次寫入：" + "、".join(phase1["written"]))
        if phase1["skipped"]:
            env.print("先前已寫（跳過）：" + "、".join(phase1["skipped"]))
    else:
        env.print("（服務端段一先前已完成，本次只落地本機 specs）")
    if landed.get("written"):
        env.print("已寫回主 spec：" + "、".join(landed["written"]))
    for warning in landed.get("warnings") or []:
        env.warn(f"警告：{warning}")
    return EXIT_OK


def push(
    args: argparse.Namespace, env: _Env, client: VaultClient, ws: Workspace
) -> int:
    async def go() -> tuple[rs.RemoteChange, str]:
        store = await open_store(client, ws, args.vault)
        if ws.find_active(args.name) is None:
            raise rs.StoreError(
                "change_not_found", f"本機沒有 active change：{args.name}"
            )
        return await align(store, ws, args.name, pull=False)

    try:
        change, action = run(go)
    except Offline as exc:
        env.print(f"push 失敗：服務不可達（{exc}）")
        return EXIT_FAIL
    except (rs.StoreError, rs.RemoteError) as exc:
        return _fail(env, "push 失敗", exc)
    if action == "behind":
        env.print(
            f"{args.name}：本機沒有未推送的修改；服務端較新（v{change.version}），"
            f"執行 pull {args.name}"
        )
    elif action == "not_active":
        env.print(f"push 失敗：{args.name} 已在服務端封存（{change.state}），不可再改")
        return EXIT_FAIL
    else:
        env.print(
            _describe_align(args.name, action, change)
            or f"{args.name}：已與服務端 v{change.version} 一致"
        )
    return EXIT_OK


def pull(
    args: argparse.Namespace, env: _Env, client: VaultClient, ws: Workspace
) -> int:
    async def go() -> tuple[rs.RemoteChange, str]:
        store = await open_store(client, ws, args.vault)
        change = await store.require_change(args.name)
        if change.state != rs.STATE_ACTIVE:
            return change, "not_active"
        state = rs.local_state(ws, change)
        if not state.safe_to_overwrite and not args.overwrite:
            raise rs.StoreError(
                "local_modified",
                f"本機 changes/{args.name} 有未推送的修改（{state.state}），未覆寫",
                hint=f"先 push {args.name}，或確認捨棄本機修改後帶 --overwrite",
            )
        rs.write_local(ws, change)
        return change, state.state

    try:
        change, previous = run(go)
    except Offline as exc:
        env.print(f"pull 失敗：服務不可達（{exc}）")
        return EXIT_FAIL
    except (rs.StoreError, rs.RemoteError) as exc:
        return _fail(env, "pull 失敗", exc)
    if previous == "not_active":
        env.print(
            f"{args.name} 已在服務端封存（{change.state}）；"
            "本機 specs 由 sync-specs 落地，不寫回 changes/"
        )
        return EXIT_OK
    env.print(f"已取回 {args.name} v{change.version}（本機原狀態：{previous}）")
    return EXIT_OK


def sync_specs(
    args: argparse.Namespace,
    env: _Env,
    client: VaultClient,
    ws: Workspace,
    now: _dt.datetime | None,
) -> int:
    clock = (lambda: now) if now is not None else (lambda: _dt.datetime.now(_dt.UTC))

    async def go() -> dict[str, Any]:
        store = await open_store(client, ws, args.vault)
        return await remote_ops.sync_specs(
            store, ws, args.name, overwrite=args.overwrite, now=clock
        )

    try:
        result = run(go)
    except Offline as exc:
        env.print(f"sync-specs 失敗：服務不可達（{exc}）")
        return EXIT_FAIL
    except (rs.StoreError, rs.RemoteError) as exc:
        if isinstance(exc, rs.StoreError):
            hint = {
                "spec_base_mismatch": remote_ops.HINT_SPEC_BASE_MISMATCH,
                "local_modified_archived": remote_ops.HINT_LOCAL_MODIFIED_ARCHIVED,
            }.get(str(exc.extra.get("hint_code") or exc.code))
            if hint:
                exc.extra.setdefault("hint", hint)
            landed = exc.extra.get("landed")
            if landed:
                env.print("已落地：" + "、".join(landed))
        return _fail(env, "sync-specs 中止", exc)
    if not result["results"]:
        env.print("沒有待落地的 change")
    for item in result["results"]:
        env.print(f"已落地 {item['name']} → {ws.archive_dir / item['archive_dir']}")
        if item["written"]:
            env.print("    已寫回主 spec：" + "、".join(item["written"]))
        if item["already_applied"]:
            env.print("    先前已寫回：" + "、".join(item["already_applied"]))
        for warning in item.get("warnings") or []:
            env.warn(f"警告：{warning}")
    return EXIT_OK


def init_remote(
    env: _Env, client: VaultClient, ws: Workspace, vault: str | None
) -> int:
    async def go() -> dict[str, Any]:
        store = await open_store(client, ws, vault)
        created = await store.ensure_index()
        changes, _ = await store.list_changes()
        mirrors = await remote_ops.push_mirrors(store, ws, changes)
        decisions = await remote_ops.push_decisions(store, ws)
        return {
            "vault": store.vault,
            "created": created,
            "mirrors": mirrors,
            "decisions": decisions,
        }

    try:
        result = run(go)
    except Offline as exc:
        env.print(f"服務端初始化失敗：服務不可達（{exc}）；本機骨架已建立")
        return EXIT_FAIL
    except (rs.StoreError, rs.RemoteError) as exc:
        return _fail(env, "服務端初始化失敗", exc)
    env.print(
        f"服務端任務層：{result['vault']}"
        + ("（新建）" if result["created"] else "（已存在）"),
        "主 spec 鏡像：推送 "
        + ("、".join(result["mirrors"]["pushed"]) or "無")
        + f"；DECISIONS 鏡像：{result['decisions']['status']}",
    )
    return EXIT_OK
