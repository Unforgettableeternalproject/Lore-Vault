"""任務層自帶的 doctor（`python -m lore_vault.tasks doctor`）。

刻意不在核心 `doctor/builtin.py` 註冊——否則核心就 import 了任務層。沿用核心的
doctor 框架（`Registry`／`CheckResult`），註冊表在這裡自建。

context：
- settings `tasks_root`：任務目錄；未設定（沒啟用任務層）→ 全部 skipped
- settings `decisions_path`：DECISIONS.md；settings `package_root`：isolation 掃描根
- settings `vault`：`tasks.snapshot_sync`／`snapshot_shape` 比對的 vault（缺省由專案目錄
  binding 推算，同 `sync`）
- settings `now`（datetime，測試注入）、`pending_apply_stale_hours`（預設 72）：
  `tasks.pending_apply_stale` 的時鐘與門檻
- resources `client`：`VaultClient`；要對服務的檢查缺少時 skipped

服務端任務內容的對帳（TASK_LAYER_MCP §6：`pending_apply_stale`／
`authorization_record_integrity`／`version_sync_agreement`／`specs_mirror_agreement`，
另加 `decisions_mirror_agreement`）經 `remote_store.RemoteStore` 讀版本化側載；
服務端還沒有 `task-index`（這個 vault 未遷移、也沒跑過 MCP init）時記為 skipped，
不把純本機的工作區判成異常。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lore_vault.doctor.framework import (
    CheckResult,
    CheckSkipped,
    DoctorContext,
    Registry,
)

from . import remote_store as rs
from . import snapshot, specs
from .archive import NOTE_DIGESTS_KEY
from .isolation import DEFAULT_PACKAGE_ROOT, check_core_isolation
from .vault_client import ServiceError, VaultClient
from .workspace import (
    SUMMARY_KEY,
    Change,
    Workspace,
    remote_enabled,
    requirement_overlap,
)

CATEGORY = "tasks"
# 試用期（OpenSpec CLI）封存、未寫 note 的 change 以此欄位註明原因
LEGACY_KEY = "legacy_archive"
# 服務端 DECISIONS 解析結果的鏡像（推送端與格式見 remote_store）
DECISIONS_KEY = rs.DECISIONS_KEY
PENDING_APPLY_STALE_HOURS = 72


def _workspace(ctx: DoctorContext) -> Workspace:
    root = ctx.settings.get("tasks_root")
    if not root:
        raise CheckSkipped("缺少設定：tasks_root（未啟用任務層）")
    decisions = ctx.settings.get("decisions_path")
    return Workspace(Path(str(root)), Path(str(decisions)) if decisions else None)


def _client(ctx: DoctorContext) -> VaultClient:
    return ctx.require("client")


def isolation(ctx: DoctorContext) -> CheckResult:
    _workspace(ctx)
    root = Path(str(ctx.settings.get("package_root") or DEFAULT_PACKAGE_ROOT))
    report = check_core_isolation(root)
    counts = {"scanned": len(report.scanned), "violations": len(report.violations)}
    if report.ok:
        return CheckResult.ok("核心未 import 任務層", counts=counts)
    return CheckResult.fail(
        "核心 import 了任務層",
        details=[f"{v.path}:{v.lineno} {v.statement}" for v in report.violations],
        counts=counts,
    )


def _service_error(exc: ServiceError) -> CheckResult:
    return CheckResult.warn(f"服務無法對帳：{exc.detail}")


def archive_note_agreement(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    archived = ws.archived()
    half = [c for c in ws.active() if c.meta.get("notes")]
    fails: list[str] = []
    warns = [
        f"{c.name}：已寫 {len(c.meta['notes'])} 則 note 但未封存"
        "（archive 半途，重跑續寫）"
        for c in half
    ]
    # 舊版 archive 沒記 note_digests：續跑無法偵測 delta 在兩次執行之間被改過
    for c in half:
        digests = c.meta.get(NOTE_DIGESTS_KEY) or {}
        missing = [k for k in c.meta["notes"] if k not in digests]
        if missing:
            warns.append(
                f"{c.name}：已寫的 note 沒記 {NOTE_DIGESTS_KEY}"
                f"（{'、'.join(missing)}），續跑偵測不到 delta 被改過；"
                "確認 delta 未改後再重跑"
            )
    to_check: list[tuple[Change, str]] = []
    legacy = 0
    for change in archived:
        note_id = change.meta.get("note_id")
        if change.meta.get(LEGACY_KEY) and not note_id:
            legacy += 1
            continue
        if not note_id:
            fails.append(f"{change.path.name}：封存目錄沒有 note_id")
            continue
        if not change.meta.get("vault"):
            fails.append(f"{change.path.name}：封存目錄沒有 vault")
            continue
        to_check.append((change, str(note_id)))
    if to_check:
        client = _client(ctx)
        try:
            for change, note_id in to_check:
                found, _ = client.get_meta(
                    str(change.meta["vault"]),
                    str(change.meta.get("space") or "dev"),
                    [note_id],
                )
                item = found.get(note_id)
                if item is None:
                    fails.append(f"{change.name}：note {note_id} 不存在")
                elif specs.change_topic(change.name) not in (item.get("topics") or []):
                    fails.append(
                        f"{change.name}：note {note_id} 的 topics 缺 "
                        f"{specs.change_topic(change.name)}"
                    )
        except ServiceError as exc:
            return _service_error(exc)
    counts = {"archived": len(archived), "legacy": legacy, "half_written": len(half)}
    if fails:
        return CheckResult.fail(
            "archive 與 note 對不上", details=fails + warns, counts=counts
        )
    if warns:
        return CheckResult.warn(
            "有 change 停在 archive 半途", details=warns, counts=counts
        )
    details = [f"{legacy} 個試用期封存未寫 note（{LEGACY_KEY}）"] if legacy else []
    return CheckResult.ok("archive 與 note 一致", details=details, counts=counts)


def requirement_overlap_check(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    overlap = requirement_overlap(ws.active())
    if overlap:
        return CheckResult.fail(
            "多個 active change 修改同一 requirement",
            details=[f"{k}：{'、'.join(v)}" for k, v in overlap.items()],
        )
    return CheckResult.ok("沒有重疊的 requirement")


def blocked_decision_resolvable(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    needed = {
        c.name: list(c.meta.get("blocked_by") or [])
        for c in ws.active()
        if c.meta.get("blocked_by")
    }
    if not needed:
        return CheckResult.ok("沒有 change 被 D 編號擋住")
    decisions = ws.decisions()
    if decisions is None:
        return CheckResult.fail(
            f"找不到 DECISIONS.md（{ws.decisions_path or '未設定 decisions_file'}）",
            details=[f"{n}：{', '.join(ds)}" for n, ds in needed.items()],
        )
    missing = [
        f"{n}：{d} 在 DECISIONS.md 沒有 ### {d} 小節"
        for n, ds in needed.items()
        for d in ds
        if d not in decisions
    ]
    if missing:
        return CheckResult.fail("blocked_by 有找不到的 D 編號", details=missing)
    return CheckResult.ok("blocked_by 的 D 編號都找得到")


def _requirement_history(ws: Workspace) -> dict[str, list[Change]]:
    """每條 requirement 依封存順序，曾寫過 note 的 change。"""
    history: dict[str, list[Change]] = {}
    for change in ws.archived():
        for key in change.meta.get("notes") or {}:
            if key != SUMMARY_KEY:
                history.setdefault(key, []).append(change)
    return history


def _chain_fails(
    client: VaultClient, vault: str, space: str, key: str, changes: list[Change]
) -> list[str]:
    """同一 (vault, space) 內：依封存順序相鄰的 note 要逐則 supersedes，
    且鏈頭只有一則。"""
    ids = [str(c.meta["notes"][key]) for c in changes]
    found, _ = client.get_meta(vault, space, ids)
    # 先逐則確認存在（含第一則）：只有一則 note 時下面的相鄰迴圈根本不執行
    fails = [
        f"{key}：{change.name} 的 note {note_id} 不存在"
        for note_id, change in zip(ids, changes, strict=True)
        if note_id not in found
    ]
    for prev, cur, change in zip(ids, ids[1:], changes[1:], strict=False):
        item = found.get(cur)
        if item is None:
            continue  # 已在存在檢查回報
        if item.get("supersedes") != prev:
            fails.append(
                f"{key}：{change.name} 的 note 應 supersedes {prev}，"
                f"實際為 {item.get('supersedes')}"
            )
    heads = [
        i["id"]
        for i in client.list_topic(vault, space, specs.requirement_topic(key))
        if not i.get("superseded_by")
    ]
    if len(heads) > 1:
        fails.append(f"{key}（{vault}）：鏈頭不只一則（{', '.join(heads)}）")
    return fails


def supersedes_chain(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    history = _requirement_history(ws)
    if not history:
        return CheckResult.ok("沒有 requirement note 需要對帳")
    client = _client(ctx)
    fails: list[str] = []
    try:
        for key, changes in history.items():
            # 鏈是各 (vault, space) 各自一條：不同 vault 的 note 不會互相 supersedes，
            # 攤平比較會把兩條獨立的鏈誤判成斷裂
            groups: dict[tuple[str, str], list[Change]] = {}
            for c in changes:
                group = (str(c.meta.get("vault")), str(c.meta.get("space") or "dev"))
                groups.setdefault(group, []).append(c)
            for (vault, space), members in groups.items():
                fails += _chain_fails(client, vault, space, key, members)
    except ServiceError as exc:
        return _service_error(exc)
    counts = {"requirements": len(history)}
    if fails:
        return CheckResult.fail("supersedes 鏈斷裂", details=fails, counts=counts)
    return CheckResult.ok("supersedes 鏈完整", counts=counts)


def spec_delta_applied(ctx: DoctorContext) -> CheckResult:
    """每條 requirement 只拿最後一個觸及它的封存 change 與主 spec 比對。"""
    ws = _workspace(ctx)
    latest: dict[str, tuple[Change, str, specs.DeltaPlan, str]] = {}
    for change in ws.archived():
        for cap, plan in change.plans().items():
            for _, name in plan.operations():
                latest[specs.requirement_key(cap, name)] = (change, cap, plan, name)
    fails = []
    for key, (change, cap, plan, name) in latest.items():
        single = specs.DeltaPlan(
            added=[b for b in plan.added if b.name == name],
            modified=[b for b in plan.modified if b.name == name],
            removed=[n for n in plan.removed if n == name],
        )
        for problem in specs.delta_applied(ws.read_main_spec(cap), single):
            fails.append(f"{key}（{change.path.name}）：{problem}")
    counts = {"requirements": len(latest)}
    if fails:
        return CheckResult.fail(
            "主 spec 與封存的 delta 不一致", details=fails, counts=counts
        )
    return CheckResult.ok("主 spec 已併入封存的 delta", counts=counts)


def dependency_exists(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    known = {c.name for c in ws.active()} | {c.name for c in ws.archived()}
    missing = [
        f"{c.name}：depends_on {d} 不存在"
        for c in ws.active() + ws.archived()
        for d in c.meta.get("depends_on") or []
        if d not in known
    ]
    if missing:
        return CheckResult.fail("depends_on 指向不存在的 change", details=missing)
    return CheckResult.ok("depends_on 都存在")


def _remote_snapshot(
    ctx: DoctorContext, ws: Workspace
) -> tuple[str, dict[str, Any] | None]:
    client = _client(ctx)
    vault = snapshot.resolve_vault(client, ws, ctx.settings.get("vault"))
    return vault, client.get_blob(vault, "dev", snapshot.SNAPSHOT_KEY)


def snapshot_sync(ctx: DoctorContext) -> CheckResult:
    """每個涉及的 vault：本機依 vault 分份重算的快照與服務端側載逐位元組相同
    （雜湊比對；分份規則同 `sync`，見 `snapshot.vault_payloads`）。

    預設 vault 已有 `task-index`（已遷移／同步模式）時改比對服務端內容算出的快照
    （`snapshot.remote_snapshot_bytes`，與 `push_remote` 同一算法），只比預設 vault。

    不同步只代表 UI 看到舊資料，所以是 warn 不是 fail；
    快照超過服務端上限、根本推不上去才是 fail。"""
    ws = _workspace(ctx)
    client = _client(ctx)
    try:
        vault = snapshot.resolve_vault(client, ws, ctx.settings.get("vault"))
        store = rs.RemoteStore(rs.vault_client_post(client), vault)
        if asyncio.run(snapshot.has_remote_index(store)):
            return _remote_snapshot_sync(client, store, ws)
    except ServiceError as exc:
        return _service_error(exc)
    except rs.RemoteUnreachable as exc:
        return CheckResult.warn(f"服務無法對帳：{exc.detail}")
    except rs.RemoteError as exc:
        return CheckResult.warn(f"服務拒絕對帳請求：{exc.message}")
    try:
        payloads = snapshot.vault_payloads(ws, client, ctx.settings.get("vault"))
    except snapshot.SnapshotTooLarge as exc:
        return CheckResult.fail(str(exc))
    except ServiceError as exc:
        return _service_error(exc)
    counts = {
        "vaults": len(payloads),
        "local_bytes": sum(len(d) for d in payloads.values()),
    }
    problems: list[str] = []
    details: list[str] = []
    for vault, local in payloads.items():
        try:
            remote = client.get_blob(vault, "dev", snapshot.SNAPSHOT_KEY)
        except ServiceError as exc:
            return _service_error(exc)
        except ValueError:
            return CheckResult.warn(
                f"{vault} 的服務端快照無法解碼，執行 sync 重推",
                details=["見 tasks.snapshot_shape"],
                counts=counts,
            )
        if remote is None:
            problems.append(f"{vault} 尚未同步任務快照（UI 任務畫面看不到），執行 sync")
        elif snapshot.digest(remote["content"]) != snapshot.digest(local):
            problems.append(
                f"{vault} 的任務快照與本機不同（本機改過但未重推），執行 sync"
            )
            details.append(f"{vault}：服務端同步於 {remote.get('updated')}")
        else:
            details.append(f"{vault}：同步於 {remote.get('updated')}")
    if problems:
        return CheckResult.warn(
            "；".join(problems), details=problems + details, counts=counts
        )
    return CheckResult.ok(
        f"{'、'.join(payloads)} 的任務快照與本機一致", details=details, counts=counts
    )


def _remote_snapshot_sync(
    client: VaultClient, store: rs.RemoteStore, ws: Workspace
) -> CheckResult:
    vault = store.vault
    try:
        expected = asyncio.run(snapshot.remote_snapshot_bytes(store, ws))
    except snapshot.SnapshotTooLarge as exc:
        return CheckResult.fail(str(exc))
    except rs.StoreError as exc:
        return CheckResult.fail(f"服務端任務內容無法解析：{exc.message}")
    counts = {"vaults": 1, "local_bytes": len(expected)}
    try:
        remote = client.get_blob(vault, "dev", snapshot.SNAPSHOT_KEY)
    except ValueError:
        return CheckResult.warn(
            f"{vault} 的服務端快照無法解碼，執行 sync 重推",
            details=["見 tasks.snapshot_shape"],
            counts=counts,
        )
    if remote is None:
        problem = f"{vault} 尚未同步任務快照（UI 任務畫面看不到），執行 sync"
        return CheckResult.warn(problem, details=[problem], counts=counts)
    if snapshot.digest(remote["content"]) != snapshot.digest(expected):
        problem = f"{vault} 的任務快照與服務端內容算出的不同，執行 sync 重推"
        return CheckResult.warn(
            problem,
            details=[problem, f"{vault}：服務端同步於 {remote.get('updated')}"],
            counts=counts,
        )
    return CheckResult.ok(
        f"{vault} 的任務快照與服務端內容一致",
        details=[f"{vault}：同步於 {remote.get('updated')}（服務端內容算法）"],
        counts=counts,
    )


def snapshot_shape(ctx: DoctorContext) -> CheckResult:
    """服務端側載內容能解析成快照 schema v1（壞掉時 UI 無法顯示）。"""
    ws = _workspace(ctx)
    try:
        vault, remote = _remote_snapshot(ctx, ws)
    except ServiceError as exc:
        return _service_error(exc)
    except ValueError:
        return CheckResult.fail("服務端快照不是合法的 base64，執行 sync 重推")
    if remote is None:
        raise CheckSkipped(f"{vault} 尚無任務快照（見 tasks.snapshot_sync）")
    errors = snapshot.shape_errors(remote["content"])
    if errors:
        return CheckResult.fail(
            "服務端任務快照格式不符，執行 sync 重推", details=errors[:20]
        )
    return CheckResult.ok("服務端任務快照格式正確")


# ── 服務端任務內容（TASK_LAYER_MCP §6）──────────────────────────────


@dataclass
class _Remote:
    """一次載入的服務端任務內容：索引狀態、能讀到的 change 文件、讀不了的項目。"""

    vault: str
    entries: dict[str, str]
    docs: dict[str, rs.RemoteChange] = field(default_factory=dict)
    bad: list[str] = field(default_factory=list)


async def _load_remote(store: rs.RemoteStore) -> _Remote:
    """索引裡每個名稱都讀 change 文件（含 `archived`：落地後文件若保留
    `apply`／delta，供鏡像倒退偵測；不存在的跳過）。沒有索引 → skipped。"""
    index, version = await store.get_index()
    if version == 0:
        raise CheckSkipped(
            f"{store.vault} 的服務端尚無任務索引（未遷移，執行 tasks migrate）"
        )
    entries = {
        str(name): str((entry or {}).get("state") or "")
        for name, entry in (index.get("changes") or {}).items()
    }
    remote = _Remote(store.vault, entries)
    for name in sorted(entries):
        try:
            change = await store.get_change(name)
        except rs.StoreError as exc:
            remote.bad.append(f"{name}：{exc.message}")
            continue
        if change is not None:
            remote.docs[name] = change
    return remote


def _remote_check(
    ctx: DoctorContext,
    run: Callable[[Workspace, rs.RemoteStore], Awaitable[CheckResult]],
) -> CheckResult:
    ws = _workspace(ctx)
    client = _client(ctx)
    try:
        vault = snapshot.resolve_vault(client, ws, ctx.settings.get("vault"))
        store = rs.RemoteStore(rs.vault_client_post(client), vault)
        return asyncio.run(run(ws, store))
    except ServiceError as exc:
        return _service_error(exc)
    except rs.RemoteUnreachable as exc:
        return CheckResult.warn(f"服務無法對帳：{exc.detail}")
    except rs.RemoteError as exc:
        return CheckResult.warn(f"服務拒絕對帳請求：{exc.message}")
    except rs.StoreError as exc:
        return CheckResult.fail(f"服務端任務內容無法解析：{exc.message}")


def _parse_utc(value: object) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _now(ctx: DoctorContext) -> _dt.datetime:
    now = ctx.settings.get("now")
    return now if isinstance(now, _dt.datetime) else _dt.datetime.now(_dt.UTC)


def _archived_stamp(change: rs.RemoteChange) -> object:
    return (change.doc.get("apply") or {}).get("archived_at") or change.meta.get(
        "archived_at"
    )


def pending_apply_stale(ctx: DoctorContext) -> CheckResult:
    """服務端 `pending_apply`（段一已寫 note、本機 specs/ 未落地）超過門檻
    （預設 72 小時）仍未落地 → warn；沒有可解析的 `archived_at` 也 warn。"""
    hours = float(
        ctx.settings.get("pending_apply_stale_hours") or PENDING_APPLY_STALE_HOURS
    )
    limit = _dt.timedelta(hours=hours)
    now = _now(ctx)

    async def run(ws: Workspace, store: rs.RemoteStore) -> CheckResult:
        remote = await _load_remote(store)
        pending = [c for c in remote.docs.values() if c.state == rs.STATE_PENDING_APPLY]
        stale: list[str] = []
        details: list[str] = []
        for change in pending:
            stamp = _archived_stamp(change)
            at = _parse_utc(stamp)
            if at is None:
                stale.append(
                    f"{change.name}：pending_apply 但沒有可解析的 archived_at"
                    f"（{stamp!r}）"
                )
                continue
            age = (now - at).total_seconds() / 3600
            if now - at > limit:
                stale.append(
                    f"{change.name}：{stamp} 段一封存，已 {age:.0f} 小時未落地"
                    "（在 stdio 執行 sync_specs）"
                )
            else:
                details.append(f"{change.name}：{stamp} 段一封存，{age:.0f} 小時")
        counts = {"pending_apply": len(pending), "stale": len(stale)}
        if stale or remote.bad:
            return CheckResult.warn(
                f"有 change 停在 pending_apply 超過 {hours:g} 小時"
                if stale
                else "有 change 文件無法讀取",
                details=stale + remote.bad + details,
                counts=counts,
            )
        return CheckResult.ok(
            f"沒有超過 {hours:g} 小時未落地的 change", details=details, counts=counts
        )

    return _remote_check(ctx, run)


def authorization_record_integrity(ctx: DoctorContext) -> CheckResult:
    """服務端每個 `requires_authorization: true` 且已寫 note 的 change，都要有
    UI session 寫入的授權紀錄（§3.3）。只看服務端文件：本機 `archive/` 的舊封存
    走 CLI `--authorized-by`，沒有 UI 紀錄是正常的。"""

    async def run(ws: Workspace, store: rs.RemoteStore) -> CheckResult:
        remote = await _load_remote(store)
        fails: list[str] = []
        warns: list[str] = []
        cli: list[str] = []
        checked = 0
        for name, change in remote.docs.items():
            meta = change.meta
            if not meta.get("requires_authorization"):
                continue
            if not (meta.get("notes") or meta.get("note_id")):
                continue  # 還沒過閘門（沒寫 note），授權與否由 archive 擋
            checked += 1
            copied = meta.get("authorization")
            if (
                isinstance(copied, dict)
                and copied.get("source") == rs.AUTHORIZATION_SOURCE_CLI
            ):
                # CLI `--authorized-by`：艾斯維爾在終端操作的已裁決路徑，沒有 UI 紀錄
                by = copied.get("authorized_by")
                if isinstance(by, str) and by.strip():
                    cli.append(f"{name}：來源 cli（{by.strip()}）")
                else:
                    fails.append(f"{name}：authorization 來源 cli 但缺少 authorized_by")
                continue
            try:
                record = await store.get_authorization(name)
            except rs.StoreError as exc:
                fails.append(f"{name}：{exc.message}")
                continue
            if record is None:
                fails.append(
                    f"{name}：已寫 note 但服務端沒有 UI 核准紀錄 "
                    f"{rs.authorization_key(name)}（授權閘門可能被繞過）"
                )
                continue
            if record.vault and record.vault != store.vault:
                fails.append(
                    f"{name}：授權紀錄的 vault 是 {record.vault}，不是 {store.vault}"
                )
                continue
            if not isinstance(copied, dict):
                warns.append(f"{name}：change meta 沒有 archive 時抄下的 authorization")
            elif (
                copied.get("change_version") != record.change_version
                or copied.get("authorized_by") != record.authorized_by
            ):
                warns.append(
                    f"{name}：授權紀錄（v{record.change_version}，"
                    f"{record.authorized_by}）與 archive 時抄下的"
                    f"（v{copied.get('change_version')}，"
                    f"{copied.get('authorized_by')}）不同"
                )
        counts = {"checked": checked}
        if fails:
            return CheckResult.fail(
                "有需授權的 change 缺少有效的 UI 核准紀錄",
                details=fails + warns + remote.bad + cli,
                counts=counts,
            )
        if warns or remote.bad:
            return CheckResult.warn(
                "授權紀錄與 change 記錄不一致" if warns else "有 change 文件無法讀取",
                details=warns + remote.bad + cli,
                counts=counts,
            )
        return CheckResult.ok(
            "需授權的 change 都有核准紀錄（UI 或 CLI）", details=cli, counts=counts
        )

    return _remote_check(ctx, run)


def version_sync_agreement(ctx: DoctorContext) -> CheckResult:
    """本機 active change 的 `remote_version`／內容與服務端版本化內容比對：
    (a) 本機版本落後（沒 pull）；(b) 版本一致但內容雜湊不同（本機改了沒推）；
    本機版本比服務端新（服務端被回退或重建）。服務端已 `pending_apply`／`archived`
    的不比（本機在 sync_specs 前本來就還在 changes/，見 pending_apply_stale）。"""

    async def run(ws: Workspace, store: rs.RemoteStore) -> CheckResult:
        remote = await _load_remote(store)
        warns: list[str] = []
        details: list[str] = []
        synced = 0
        local_names = set()
        for local in ws.active():
            name = local.name
            local_names.add(name)
            if local.meta_error:
                warns.append(f"{name}：本機 .openspec.yaml 無法解析")
                continue
            raw = local.meta.get(rs.REMOTE_VERSION_KEY)
            recorded = (
                raw if isinstance(raw, int) and not isinstance(raw, bool) else None
            )
            change = remote.docs.get(name)
            if change is None:
                if recorded is None:
                    warns.append(f"{name}：尚未推送到服務端（執行 tasks migrate）")
                else:
                    warns.append(
                        f"{name}：本機記錄同步過 v{recorded}，服務端卻沒有這個 change"
                    )
                continue
            if change.state != rs.STATE_ACTIVE:
                details.append(f"{name}：服務端為 {change.state}，不比對工作副本")
                continue
            digest = rs.content_digest(rs.local_doc(local))
            modified = digest != local.meta.get(rs.REMOTE_DIGEST_KEY)
            if recorded is None:
                warns.append(
                    f"{name}：本機沒有 remote_version，服務端為 v{change.version}"
                    "（執行 pull，或 tasks migrate 回填）"
                )
            elif recorded < change.version:
                warns.append(
                    f"{name}：本機 v{recorded} 落後服務端 v{change.version}"
                    "（執行 pull，可能漏看別人的修改）"
                    + ("；本機另有未推送的修改" if modified else "")
                )
            elif recorded > change.version:
                warns.append(
                    f"{name}：本機記錄 v{recorded} 比服務端 v{change.version} 新"
                    "（服務端被回退或重建？）"
                )
            elif digest != change.digest():
                warns.append(
                    f"{name}：版本一致（v{recorded}）但本機內容與服務端不同"
                    "（本機修改未成功推送）"
                )
            else:
                synced += 1
        remote_only = sorted(
            n
            for n, c in remote.docs.items()
            if c.state == rs.STATE_ACTIVE and n not in local_names
        )
        if remote_only:
            details.append("服務端有本機沒有的 change：" + "、".join(remote_only))
        counts = {"local_active": len(local_names), "in_sync": synced}
        if warns or remote.bad:
            return CheckResult.warn(
                "本機工作副本與服務端版本不一致" if warns else "有 change 文件無法讀取",
                details=warns + remote.bad + details,
                counts=counts,
            )
        return CheckResult.ok(
            "本機工作副本與服務端版本一致", details=details, counts=counts
        )

    return _remote_check(ctx, run)


def _single(plan: specs.DeltaPlan, name: str) -> specs.DeltaPlan:
    return specs.DeltaPlan(
        added=[b for b in plan.added if b.name == name],
        modified=[b for b in plan.modified if b.name == name],
        removed=[n for n in plan.removed if n == name],
    )


def specs_mirror_agreement(ctx: DoctorContext) -> CheckResult:
    """主 spec 鏡像對帳，兩條規則：

    - 落後（warn）：鏡像與本機 `specs/<cap>/spec.md` 現值不同（或沒有鏡像）。
      有 `pending_apply` change 正在合併的 capability 不比——鏡像本來就領先 git
    - 倒退（fail）：鏡像缺少服務端最後一個封存（`pending_apply`／`archived`）change
      對該 requirement 併入的內容。這是沒 git pull 的機器跑 stdio validate、把舊內容
      推回鏡像的情況；下一次 archive 會在掉了內容的 base 上合併，再由 sync_specs
      寫進 git，所以比落後嚴重"""

    async def run(ws: Workspace, store: rs.RemoteStore) -> CheckResult:
        remote = await _load_remote(store)
        local = rs.local_main_specs(ws)
        caps = set(local)
        for change in ws.active():
            caps |= set(change.delta_files())
        landed: list[rs.RemoteChange] = []
        pending: dict[str, str] = {}
        for change in remote.docs.values():
            if change.state == rs.STATE_ACTIVE:
                caps |= set(change.deltas)
                continue
            landed.append(change)
            if change.state == rs.STATE_PENDING_APPLY:
                for cap in (change.doc.get("apply") or {}).get("merged_specs") or {}:
                    pending.setdefault(str(cap), change.name)
        latest: dict[str, tuple[rs.RemoteChange, str, specs.DeltaPlan, str]] = {}
        for change in sorted(landed, key=lambda c: (str(_archived_stamp(c)), c.name)):
            if change.meta.get("skip_specs"):
                continue
            for cap, plan in change.plans().items():
                for _, req in plan.operations():
                    latest[specs.requirement_key(cap, req)] = (change, cap, plan, req)
        wanted = {c for c in caps | set(pending) if rs.NAME_RE.match(c)}
        wanted |= {cap for _, cap, _, _ in latest.values() if rs.NAME_RE.match(cap)}
        mirrors = {cap: await store.get_mirror(cap) for cap in sorted(wanted)}
        lag: list[str] = []
        details: list[str] = []
        for cap in sorted(c for c in caps if rs.NAME_RE.match(c)):
            if cap in pending:
                details.append(f"{cap}：change {pending[cap]} 待落地，鏡像領先 git")
                continue
            mirror = mirrors.get(cap)
            if mirror is None:
                lag.append(f"{cap}：服務端沒有鏡像（在 stdio 執行 validate 推送）")
            elif not mirror.same_content(cap in local, local.get(cap)):
                lag.append(
                    f"{cap}：鏡像（v{mirror.version}，{mirror.source}）與本機 "
                    "specs/ 現值不同（鏡像落後，或本機尚未 git pull）"
                )
        regress: list[str] = []
        for key, (change, cap, plan, req) in sorted(latest.items()):
            mirror = mirrors.get(cap)
            text = mirror.text if mirror is not None and mirror.exists else None
            for problem in specs.delta_applied(text, _single(plan, req)):
                regress.append(
                    f"{key}：鏡像缺少 {change.name}（{change.state}）併入的內容"
                    f"（鏡像被舊內容倒退？）：{problem}"
                )
        counts = {"capabilities": len(caps), "pending": len(pending)}
        if regress:
            return CheckResult.fail(
                "主 spec 鏡像倒退，缺少已封存 change 的內容",
                details=regress + lag + remote.bad,
                counts=counts,
            )
        if lag or remote.bad:
            return CheckResult.warn(
                "主 spec 鏡像與本機不一致" if lag else "有 change 文件無法讀取",
                details=lag + remote.bad + details,
                counts=counts,
            )
        return CheckResult.ok("主 spec 鏡像與本機一致", details=details, counts=counts)

    return _remote_check(ctx, run)


def decisions_mirror_agreement(ctx: DoctorContext) -> CheckResult:
    """本機 DECISIONS.md 解析結果與服務端 `task-decisions` 鏡像一致
    （HTTP 模式判定 `blocked_by` 靠這份）。格式由推送端（MCP-T6）定，這裡只讀
    `decisions: {Dn: bool}`，其餘欄位缺漏都容忍。"""

    async def run(ws: Workspace, store: rs.RemoteStore) -> CheckResult:
        local = ws.decisions()
        if local is None:
            raise CheckSkipped(
                f"找不到 DECISIONS.md（{ws.decisions_path or '未設定 decisions_file'}）"
            )
        blob = await store.get_blob(DECISIONS_KEY)
        if blob is None and remote_enabled(ws.root):
            return CheckResult.warn(
                f"同步模式（config remote: true）但 {store.vault} 的服務端沒有 "
                f"{DECISIONS_KEY} 鏡像，HTTP 模式判定不了 blocked_by；執行 sync 推送"
            )
        if blob is None:
            raise CheckSkipped(
                f"{store.vault} 的服務端尚無 {DECISIONS_KEY} 鏡像（由 stdio 推送）"
            )
        try:
            data = json.loads(blob.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        mirrored = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(mirrored, dict):
            return CheckResult.warn(
                f"{DECISIONS_KEY} 的格式無法辨識（缺少 decisions），在 stdio 重推"
            )
        diffs = []
        for key in sorted(set(local) | {str(k) for k in mirrored}):
            mine = local.get(key)
            theirs = mirrored.get(key)
            if mine is None or not isinstance(theirs, bool) or mine != theirs:
                diffs.append(f"{key}：本機 {_decision(mine)}，鏡像 {_decision(theirs)}")
        details = []
        if isinstance(data, dict) and data.get("source_digest"):
            details.append(f"鏡像 source_digest：{data['source_digest']}")
        counts = {"local": len(local), "mirrored": len(mirrored)}
        if diffs:
            return CheckResult.warn(
                "DECISIONS 鏡像與本機不同（在 stdio 重推）",
                details=diffs + details,
                counts=counts,
            )
        return CheckResult.ok(
            "DECISIONS 鏡像與本機一致", details=details, counts=counts
        )

    return _remote_check(ctx, run)


def _decision(value: object) -> str:
    if value is None:
        return "沒有"
    if isinstance(value, bool):
        return "已解除" if value else "未解除"
    return f"格式不符（{value!r}）"


def default_registry() -> Registry:
    registry = Registry()
    for name, func, description in (
        (
            "tasks.isolation",
            isolation,
            "核心（tasks/ 與 mcp/task_plugin.py 以外）零 import 任務層",
        ),
        (
            "tasks.archive_note_agreement",
            archive_note_agreement,
            "封存目錄的 note_id 在 Lore Vault 存在且 topics 含 change:<name>",
        ),
        (
            "tasks.requirement_overlap",
            requirement_overlap_check,
            "同一 requirement 不可同時被多個 active change 修改",
        ),
        (
            "tasks.blocked_decision_resolvable",
            blocked_decision_resolvable,
            "blocked_by 的 D 編號在 DECISIONS.md 找得到",
        ),
        (
            "tasks.supersedes_chain",
            supersedes_chain,
            "同一 requirement 的 note 依封存順序以 supersedes 串成單一鏈",
        ),
        (
            "tasks.spec_delta_applied",
            spec_delta_applied,
            "主 spec 已併入最後一個封存 delta 的內容",
        ),
        (
            "tasks.dependency_exists",
            dependency_exists,
            "depends_on 的 change 存在於 changes/ 或 archive/",
        ),
        (
            "tasks.snapshot_sync",
            snapshot_sync,
            "服務端的任務快照（UI 任務畫面）與本機重算結果一致",
        ),
        (
            "tasks.snapshot_shape",
            snapshot_shape,
            "服務端的任務快照能解析成快照 schema v1",
        ),
        (
            "tasks.pending_apply_stale",
            pending_apply_stale,
            "服務端 pending_apply 的 change 未超過門檻（預設 72 小時）仍未落地",
        ),
        (
            "tasks.authorization_record_integrity",
            authorization_record_integrity,
            "需授權且已寫 note 的 change 在服務端有 UI session 的核准紀錄",
        ),
        (
            "tasks.version_sync_agreement",
            version_sync_agreement,
            "本機 remote_version／內容與服務端版本化內容一致",
        ),
        (
            "tasks.specs_mirror_agreement",
            specs_mirror_agreement,
            "主 spec 鏡像與本機 specs/ 一致，且未倒退掉已封存 change 的內容",
        ),
        (
            "tasks.decisions_mirror_agreement",
            decisions_mirror_agreement,
            "本機 DECISIONS 解析結果與服務端 task-decisions 鏡像一致",
        ),
    ):
        registry.register(name, CATEGORY, description)(func)
    return registry
