"""任務層對服務端版本化內容的共用動作（MCP `tasks` 工具與 stdio CLI 共用）。

只依賴 `remote_store.RemoteStore`（傳輸由呼叫端注入）與本機 `Workspace`（stdio 才有；
HTTP 為 None），不綁 MCP 的 `Shell`：

- `remote_workspace`：驗證／推導用的工作區（主 spec 讀鏡像，DECISIONS 依
  `remote_store.resolve_decisions`：本機優先、其次鏡像）
- `push_mirrors`／`push_decisions`：stdio 把本機 `specs/` 與 DECISIONS 解析結果推成鏡像
- `archive_plan`／`archive_execute`：archive 段一（§1.4）；授權閘門由呼叫端先做
  （MCP 讀 UI 授權紀錄；CLI 是 `--authorized-by`，見 `cli_authorization`）
- `sync_specs`：archive 段二（落地），只能在有本機 repo 的 stdio 執行

`archive_execute` 在 worker thread 跑同步的 note 寫入（`archive.write_notes`，經
`remote_store.ThreadBridgeClient` 回到事件迴圈），呼叫端必須在 anyio 事件迴圈內
（MCP 本來就是；CLI 用 `anyio.run`）。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import anyio
import anyio.from_thread
import anyio.to_thread
import yaml

from . import remote_store as rs
from . import specs
from .archive import (
    DEFAULT_AUTHOR,
    ArchiveError,
    ArchiveResult,
    _check_written_digests,
    _removed_keys,
    _requirement_items,
    check_incomplete,
    write_notes,
)
from .vault_client import ServiceError as HookServiceError
from .workspace import (
    META_FILE,
    SPACE_DEV,
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    Workspace,
    atomic_write_text,
    derive_status,
    read_yaml,
    trial_merge,
    validate_change,
)

HINT_SPEC_BASE_MISMATCH = (
    "本機 specs/<capability>/spec.md 與封存時的基準不一致（git 已被別人改過）："
    "先確認那次修改，還原到封存時的內容、或把 pull 取回的 apply.merged_specs 手動合併"
    "進本機主 spec 後重跑 sync_specs"
)
HINT_LOCAL_MODIFIED_ARCHIVED = (
    "change 已在服務端封存，但本機工作副本有未推送的修改：確認可以捨棄後帶 overwrite"
    "（CLI 為 --overwrite）重跑 sync_specs"
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_blocked_by(changes: Sequence[rs.RemoteChange]) -> bool:
    return any(c.meta.get("blocked_by") for c in changes)


async def remote_workspace(
    store: rs.RemoteStore,
    local: Workspace | None,
    changes: list[rs.RemoteChange],
    archived: set[str],
    mirror_for: Sequence[rs.RemoteChange] = (),
) -> rs.RemoteWorkspace:
    """`local`：stdio 的本機工作區（HTTP 為 None）；`mirror_for`：要讀主 spec
    鏡像的 change（validate／archive；list 不需要）。DECISIONS 鏡像只在有 change
    帶 `blocked_by` 時才讀。"""
    decisions = None
    if local is not None:
        decisions = local.decisions()
        archived = archived | {c.name for c in local.archived()}
    if decisions is None and _has_blocked_by(changes):
        decisions = await rs.resolve_decisions(store, None)
    caps = sorted({cap for c in mirror_for for cap in c.deltas})
    mirrors = {}
    for cap in caps:
        mirror = await store.get_mirror(cap)
        if mirror is not None:
            mirrors[cap] = mirror
    return rs.RemoteWorkspace(
        root=PurePosixPath("remote"),  # type: ignore[arg-type]
        changes=changes,
        mirrors=mirrors,
        archived_names=archived,
        decisions_map=decisions,
    )


async def push_mirrors(
    store: rs.RemoteStore, ws: Workspace, changes: list[rs.RemoteChange]
) -> dict[str, Any]:
    """stdio：把本機 `specs/` 推成鏡像（同內容不推；
    pending_apply 合併中的 capability 跳過）。"""
    local = rs.local_main_specs(ws)
    caps = set(local) | {cap for c in changes for cap in c.deltas}
    for change in ws.active():
        caps |= set(change.delta_files())
    pending: dict[str, str] = {}
    for change in changes:
        if change.state == rs.STATE_PENDING_APPLY:
            for cap in (change.doc.get("apply") or {}).get("merged_specs") or {}:
                pending.setdefault(cap, change.name)
    report: dict[str, Any] = {
        "pushed": [],
        "unchanged": [],
        "skipped": {},
        "conflicts": [],
    }
    for cap in sorted(c for c in caps if rs.NAME_RE.match(c)):
        if cap in pending:
            report["skipped"][cap] = (
                f"change {pending[cap]} 已封存待落地，鏡像保留併入後內容"
            )
            continue
        exists = cap in local
        text = local.get(cap)
        mirror = await store.get_mirror(cap)
        if mirror is not None and mirror.same_content(exists, text):
            report["unchanged"].append(cap)
            continue
        try:
            await store.put_mirror(
                cap,
                exists=exists,
                text=text,
                source=rs.MIRROR_SOURCE_STDIO,
                expected_version=mirror.version if mirror else 0,
            )
        except rs.RemoteError as exc:
            if exc.code != "version_conflict":
                raise
            report["conflicts"].append(cap)
            continue
        report["pushed"].append(cap)
    return report


async def push_decisions(store: rs.RemoteStore, ws: Workspace) -> dict[str, Any]:
    """stdio：把本機 DECISIONS.md 的解析結果推成 `task-decisions`。
    本機沒有檔案時不推（不會用空鏡像蓋掉其他機器推的內容）；同內容不推。"""
    source = rs.decisions_source(ws)
    if source is None:
        return {"status": "skipped", "reason": "本機沒有 DECISIONS.md"}
    decisions, digest = source
    mirror = await store.get_decisions()
    if (
        mirror is not None
        and mirror.decisions == decisions
        and mirror.source_digest == digest
    ):
        return {"status": "unchanged"}
    await store.put_decisions(decisions, digest)
    return {"status": "pushed", "decisions": len(decisions)}


# ── archive 段一 ────────────────────────────────────────────────────


def rejected(exc: ArchiveError) -> rs.StoreError:
    return rs.StoreError("archive_rejected", str(exc), details=list(exc.details))


def cli_authorization(
    authorized_by: str, change_version: int, now: datetime
) -> dict[str, Any]:
    """CLI `--authorized-by` 記進 meta `authorization` 的內容（來源標 `cli`）。"""
    return {
        "authorized_by": authorized_by,
        "authorized_at": utc_stamp(now),
        "change_version": change_version,
        "source": rs.AUTHORIZATION_SOURCE_CLI,
    }


def reconcile_mirror_applying(
    change: rs.RemoteChange, ws: rs.RemoteWorkspace, applied: list[str]
) -> None:
    """續跑：`mirror_applying` 記的鏡像若已是記錄的內容（推進成功、還沒記進
    `mirror_applied_caps` 就中斷），補記為已套用（只改記憶體，執行時才落地）。"""
    applying = change.meta.get(rs.MIRROR_APPLYING_KEY)
    if not isinstance(applying, dict) or not applying:
        return
    for cap, digest in applying.items():
        mirror = ws.mirrors.get(str(cap))
        if mirror is None or not mirror.exists or sha256(mirror.text or "") != digest:
            return
    for cap in applying:
        if cap not in applied:
            applied.append(str(cap))
    change.meta[rs.MIRROR_APPLIED_KEY] = list(applied)
    change.meta.pop(rs.MIRROR_APPLYING_KEY, None)


async def archive_plan(
    store: rs.RemoteStore,
    change: rs.RemoteChange,
    *,
    local: Workspace | None,
    authorized_by: str | None,
    authorized_version: int | None,
    allow_incomplete: bool,
    reason: str | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, rs.Mirror]]:
    """archive 段一的全驗（同本機 archive 第 1b～2 步，主 spec 讀鏡像）。
    回傳 (規劃, 尚待推進的 {cap: 併入後全文}, 讀到的鏡像)。只讀不寫。"""
    meta = change.meta
    try:
        check_incomplete(change, allow_incomplete)
    except ArchiveError as exc:
        raise rejected(exc) from None
    changes, archived = await store.list_changes()
    changes = [c for c in changes if c.name != change.name] + [change]
    rws = await remote_workspace(store, local, changes, archived, [change])
    applied = list(meta.get(rs.MIRROR_APPLIED_KEY) or [])
    reconcile_mirror_applying(change, rws, applied)
    skip_specs = bool(meta.get("skip_specs"))
    missing = [] if skip_specs else rws.missing_mirrors(change)
    if missing:
        raise rs.StoreError(
            "archive_rejected",
            f"{change.name} 未通過 validate：服務端沒有主 spec 鏡像",
            details=[f"{cap}：缺少主 spec 鏡像" for cap in missing],
            hint_code="mirror_missing",
        )
    errors = validate_change(change, rws, rws.active(), skip=applied)
    if errors:
        raise rs.StoreError(
            "archive_rejected", f"{change.name} 未通過 validate", details=errors
        )
    status, reasons = derive_status(change, rws)
    if status in (STATUS_BLOCKED, STATUS_UNKNOWN):
        raise rs.StoreError(
            "archive_rejected",
            f"{change.name} 狀態為「{status}」，不可封存",
            details=reasons,
        )
    merged: dict[str, str] = {}
    if not skip_specs:
        merged, merge_errors = trial_merge(change, rws, skip=applied)
        if merge_errors:
            raise rs.StoreError(
                "archive_rejected",
                f"{change.name} delta 併回試算失敗",
                details=merge_errors,
            )
    try:
        _check_written_digests(change, dict(meta.get("notes") or {}))
    except ArchiveError as exc:
        raise rejected(exc) from None
    caps = sorted(set(merged) | set(applied))
    plan = {
        "name": change.name,
        "vault": store.vault,
        "version": change.version,
        "requires_authorization": bool(meta.get("requires_authorization")),
        "authorized_by": authorized_by,
        "authorized_version": authorized_version,
        "incomplete": meta.get("incomplete_at_archive"),
        "reason": reason or None,
        "requirements": [k for k, *_ in _requirement_items(change)],
        "removed": _removed_keys(change),
        "already_written": sorted(meta.get("notes") or {}),
        "capabilities": caps,
        "mirror_versions": {cap: rws.mirrors[cap].version for cap in caps},
        "merged_sha256": {
            cap: sha256(merged.get(cap) or rws.mirrors[cap].text or "") for cap in caps
        },
    }
    return plan, merged, rws.mirrors


async def archive_execute(
    store: rs.RemoteStore,
    change: rs.RemoteChange,
    merged: dict[str, str],
    mirrors: dict[str, rs.Mirror],
    *,
    authorized_by: str | None,
    authorization: dict[str, Any] | None,
    reason: str | None,
    author: str | None,
    now: datetime,
) -> dict[str, Any]:
    """段一執行：寫 note（可續跑）→ 鏡像推進（write-ahead）→ `pending_apply`。

    `authorization`：archive 開始時抄進 meta `authorization` 的內容（已有就不動，
    續跑以第一次的紀錄為準）。回傳 `{name, version, note_id, written, skipped,
    capabilities}`。"""
    meta = change.meta
    name = change.name
    vault = store.vault
    if meta.get("vault") not in (None, vault):
        raise rs.StoreError(
            "archive_rejected",
            f"vault 與先前記錄不同：{meta.get('vault')} → {vault}",
        )
    meta["vault"] = vault
    if authorization is not None and not isinstance(meta.get("authorization"), dict):
        meta["authorization"] = authorization
    if reason:
        meta["archive_reason"] = reason
    await store.save_change(change)
    result = ArchiveResult(name=name, destination="", note_id="", vault=vault)
    client = rs.ThreadBridgeClient(store)

    def write() -> None:
        change.saver = lambda: anyio.from_thread.run(store.save_change, change)
        try:
            write_notes(
                change,
                client,  # type: ignore[arg-type]
                vault,
                SPACE_DEV,
                authorized_by=authorized_by,
                author=author or DEFAULT_AUTHOR,
                result=result,
            )
        finally:
            change.saver = None

    try:
        await anyio.to_thread.run_sync(write)
    except ArchiveError as exc:
        raise rejected(exc) from None
    except HookServiceError as exc:
        raise rs.StoreError(
            "archive_write_failed",
            "Lore Vault 寫入失敗，change 留在 active（重跑會跳過已寫的 note）："
            + exc.detail,
            written=sorted(meta.get("notes") or {}),
        ) from None
    # 段一：鏡像推進成併入後內容（write-ahead：先記雜湊再寫，續跑認得出）
    applied = list(meta.get(rs.MIRROR_APPLIED_KEY) or [])
    versions = {cap: mirrors[cap].version for cap in applied if cap in mirrors}
    for cap, text in merged.items():
        meta[rs.MIRROR_APPLYING_KEY] = {cap: sha256(text)}
        await store.save_change(change)
        try:
            versions[cap] = await store.put_mirror(
                cap,
                exists=True,
                text=text,
                source=f"archive:{name}",
                expected_version=mirrors[cap].version,
            )
        except rs.RemoteError as exc:
            if exc.code != "version_conflict":
                raise
            raise rs.StoreError(
                "mirror_changed",
                f"主 spec 鏡像 {cap} 在驗證後被更新，鏡像未推進；note 已寫入，"
                "重跑 archive 會依新鏡像重新驗證",
            ) from None
        applied.append(cap)
        meta[rs.MIRROR_APPLIED_KEY] = list(applied)
        meta.pop(rs.MIRROR_APPLYING_KEY, None)
        await store.save_change(change)
    merged_specs = {
        cap: merged.get(cap) or (mirrors[cap].text or "") for cap in applied
    }
    stamp = utc_stamp(now)
    meta["archived_at"] = stamp
    change.doc["state"] = rs.STATE_PENDING_APPLY
    change.doc["apply"] = {
        "archived_at": stamp,
        "merged_specs": dict(sorted(merged_specs.items())),
        "mirror_versions": dict(sorted(versions.items())),
    }
    await store.save_change(change)
    await store.set_index_state(name, rs.STATE_PENDING_APPLY)
    return {
        "name": name,
        "version": change.version,
        "note_id": meta.get("note_id"),
        "written": result.written,
        "skipped": result.skipped,
        "capabilities": sorted(merged_specs),
    }


# ── archive 段二：sync_specs ─────────────────────────────────────────


def _norm(text: str | None) -> str | None:
    return None if text is None else specs.normalize_text(text)


def _archive_date(change: rs.RemoteChange, now: datetime) -> str:
    apply = change.doc.get("apply") or {}
    stamp = apply.get("archived_at") or change.meta.get("archived_at")
    if isinstance(stamp, str) and len(stamp) >= 10:
        return stamp[:10]
    return now.astimezone(UTC).date().isoformat()


def _apply_order(change: rs.RemoteChange) -> tuple[str, str]:
    stamp = (change.doc.get("apply") or {}).get("archived_at")
    return (str(stamp or ""), change.name)


async def sync_specs(
    store: rs.RemoteStore,
    ws: Workspace,
    name: str | None = None,
    *,
    overwrite: bool = False,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    """archive 段二（§1.4）：把 `pending_apply` change 的 `apply.merged_specs` 寫進
    本機 `specs/<cap>/spec.md`、本機 change 目錄換成 `changes/archive/<date>-<name>/`
    封存記錄（本機沒有工作副本時依服務端內容建），狀態改 `archived`，最後重推鏡像。

    依 `apply.archived_at` 先後處理（後封存的 change 併入結果疊在先封存的之上）；
    任一個失敗就停在那裡（已落地的保留），錯誤附 `landed`。"""
    changes, _ = await store.list_changes()
    pending = sorted(
        (c for c in changes if c.state == rs.STATE_PENDING_APPLY), key=_apply_order
    )
    if name is not None:
        rs.check_name(name)
        pending = [c for c in pending if c.name == name]
        if not pending:
            found = await store.get_change(name)
            if found is None:
                raise rs.StoreError(
                    "change_not_found",
                    f"服務端沒有 change {name}（vault {store.vault}）",
                )
            if found.state != rs.STATE_ARCHIVED:
                raise rs.StoreError(
                    "change_not_pending_apply",
                    f"change {name} 狀態為 {found.state}，不是待落地（pending_apply）",
                )
    results: list[dict[str, Any]] = []
    for change in pending:
        try:
            results.append(await land(store, ws, change, overwrite=overwrite, now=now))
        except rs.StoreError as exc:
            exc.extra.setdefault("landed", [r["name"] for r in results])
            raise
    refreshed, _ = await store.list_changes()
    mirrors = await push_mirrors(store, ws, refreshed)
    return {"results": results, "mirrors": mirrors}


async def land(
    store: rs.RemoteStore,
    ws: Workspace,
    change: rs.RemoteChange,
    *,
    overwrite: bool,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    """單一 change 的落地。順序與續跑：

    1. 全部 capability 先核對本機主 spec：已是併入後內容（或已記在
       `apply.applied_caps`）→ 已落地；否則以本機主 spec 現值重算 delta 併入，
       結果必須與 `apply.merged_specs` 一致（＝本機仍是封存時的基準），任一不符
       就拒絕、一個檔案都不寫
    2. 逐一寫回：寫前在服務端記 `apply.applying`（write-ahead），寫完移進
       `apply.applied_caps`；寫完、還沒記就中斷時，續跑由第 1 步認出已落地
    3. 封存記錄：暫存目錄寫完再改名；目的目錄已存在且 note_id 相同＝上次已建
    4. 移除本機工作副本，服務端狀態與索引改 `archived`
    """
    apply = change.doc.get("apply")
    if not isinstance(apply, dict):
        raise rs.StoreError(
            "invalid_remote_content", f"change {change.name} 缺少 apply（段一結果）"
        )
    working = ws.changes_dir / change.name
    local = ws.find_active(change.name)
    if local is not None and not overwrite:
        state = rs.local_state(ws, change)
        if not state.safe_to_overwrite:
            raise rs.StoreError(
                "local_modified",
                f"本機 changes/{change.name} 有未推送的修改（{state.state}），"
                "但 change 已在服務端封存",
                hint_code="local_modified_archived",
                local_state=state.state,
            )
    merged = {
        str(cap): text
        for cap, text in sorted((apply.get("merged_specs") or {}).items())
        if isinstance(text, str)
    }
    recorded = [str(c) for c in apply.get(rs.APPLY_APPLIED_KEY) or []]
    already = [
        cap
        for cap in merged
        if cap in recorded or _norm(ws.read_main_spec(cap)) == _norm(merged[cap])
    ]
    todo = [cap for cap in merged if cap not in already]
    trial: dict[str, str] = {}
    if todo:
        skip = [c for c in change.deltas if c not in todo]
        trial, errors = trial_merge(change, ws, skip=skip)
        mismatched = [
            cap
            for cap in todo
            if cap not in trial or _norm(trial[cap]) != _norm(merged[cap])
        ]
        if mismatched:
            raise rs.StoreError(
                "spec_base_mismatch",
                f"{change.name}：本機主 spec 與封存時的基準不一致，未落地",
                details=[f"{cap}：specs/{cap}/spec.md 已被修改" for cap in mismatched]
                + errors,
                capabilities=mismatched,
            )
    applied = list(dict.fromkeys(recorded + already))
    for cap in todo:
        text = trial[cap]
        apply[rs.APPLY_APPLYING_KEY] = {cap: sha256(text)}
        await store.save_change(change)
        atomic_write_text(ws.main_spec_path(cap), text)
        applied.append(cap)
        apply[rs.APPLY_APPLIED_KEY] = list(applied)
        apply.pop(rs.APPLY_APPLYING_KEY, None)
        await store.save_change(change)
    dest = ws.archive_dir / f"{_archive_date(change, now())}-{change.name}"
    note_id = change.meta.get("note_id")
    if dest.exists():
        try:
            dest_meta = read_yaml(dest / META_FILE)
        except (OSError, ValueError, yaml.YAMLError):
            dest_meta = {}
        if dest_meta.get("note_id") != note_id:
            raise rs.StoreError(
                "archive_dir_exists",
                f"封存目錄 {dest.name} 已存在且不是這個 change 的封存記錄",
            )
        created = False
    else:
        ws.archive_dir.mkdir(parents=True, exist_ok=True)
        # 封存記錄記錄的是落地後的狀態
        change.doc["state"] = rs.STATE_ARCHIVED
        try:
            rs.write_archive_record(ws, change, dest)
        finally:
            change.doc["state"] = rs.STATE_PENDING_APPLY
        created = True
    removed = False
    if working.is_dir():
        shutil.rmtree(working)
        removed = True
    change.doc["state"] = rs.STATE_ARCHIVED
    apply["landed_at"] = utc_stamp(now())
    await store.save_change(change)
    await store.set_index_state(change.name, rs.STATE_ARCHIVED)
    return {
        "name": change.name,
        "written": todo,
        "already_applied": already,
        "archive_dir": dest.name,
        "archive_created": created,
        "working_copy_removed": removed,
    }
