"""任務層自帶的 doctor（`python -m lore_vault.tasks doctor`）。

刻意不在核心 `doctor/builtin.py` 註冊——否則核心就 import 了任務層。沿用核心的
doctor 框架（`Registry`／`CheckResult`），註冊表在這裡自建。

context：
- settings `tasks_root`：任務目錄；未設定（沒啟用任務層）→ 全部 skipped
- settings `decisions_path`：DECISIONS.md；settings `package_root`：isolation 掃描根
- settings `vault`：`tasks.snapshot_sync`／`snapshot_shape` 比對的 vault（缺省由專案目錄
  binding 推算，同 `sync`）
- resources `client`：`VaultClient`；要對服務的檢查缺少時 skipped
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lore_vault.doctor.framework import (
    CheckResult,
    CheckSkipped,
    DoctorContext,
    Registry,
)

from . import snapshot, specs
from .archive import NOTE_DIGESTS_KEY
from .isolation import DEFAULT_PACKAGE_ROOT, check_core_isolation
from .vault_client import ServiceError, VaultClient
from .workspace import SUMMARY_KEY, Change, Workspace, requirement_overlap

CATEGORY = "tasks"
# 試用期（OpenSpec CLI）封存、未寫 note 的 change 以此欄位註明原因
LEGACY_KEY = "legacy_archive"


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

    不同步只代表 UI 看到舊資料（本機永遠是真相來源），所以是 warn 不是 fail；
    快照超過服務端上限、根本推不上去才是 fail。"""
    ws = _workspace(ctx)
    client = _client(ctx)
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


def default_registry() -> Registry:
    registry = Registry()
    for name, func, description in (
        ("tasks.isolation", isolation, "核心（tasks/ 以外）零 import 任務層"),
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
    ):
        registry.register(name, CATEGORY, description)(func)
    return registry
