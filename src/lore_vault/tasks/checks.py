"""任務層自帶的 doctor（`python -m lore_vault.tasks doctor`）。

刻意不在核心 `doctor/builtin.py` 註冊——否則核心就 import 了任務層。沿用核心的
doctor 框架（`Registry`／`CheckResult`），註冊表在這裡自建。

context：
- settings `tasks_root`：任務目錄；未設定（沒啟用任務層）→ 全部 skipped
- settings `decisions_path`：DECISIONS.md；settings `package_root`：isolation 掃描根
- resources `client`：`VaultClient`；兩項要對服務的檢查缺少時 skipped
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

from . import specs
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


def supersedes_chain(ctx: DoctorContext) -> CheckResult:
    ws = _workspace(ctx)
    history = _requirement_history(ws)
    if not history:
        return CheckResult.ok("沒有 requirement note 需要對帳")
    client = _client(ctx)
    fails: list[str] = []
    try:
        for key, changes in history.items():
            ids = [str(c.meta["notes"][key]) for c in changes]
            by_vault: dict[tuple[str, str], list[str]] = {}
            for c, i in zip(changes, ids, strict=True):
                vault = (str(c.meta.get("vault")), str(c.meta.get("space") or "dev"))
                by_vault.setdefault(vault, []).append(i)
            found: dict[str, Any] = {}
            for (vault, space), vids in by_vault.items():
                got, _ = client.get_meta(vault, space, vids)
                found.update(got)
            for prev, cur, change in zip(ids, ids[1:], changes[1:], strict=False):
                item = found.get(cur)
                if item is None:
                    fails.append(f"{key}：{change.name} 的 note {cur} 不存在")
                elif item.get("supersedes") != prev:
                    fails.append(
                        f"{key}：{change.name} 的 note 應 supersedes {prev}，"
                        f"實際為 {item.get('supersedes')}"
                    )
            vault, space = next(iter(by_vault))
            heads = [
                i["id"]
                for i in client.list_topic(vault, space, specs.requirement_topic(key))
                if not i.get("superseded_by")
            ]
            if len(heads) > 1:
                fails.append(f"{key}：鏈頭不只一則（{', '.join(heads)}）")
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
    ):
        registry.register(name, CATEGORY, description)(func)
    return registry
