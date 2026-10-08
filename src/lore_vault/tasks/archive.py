"""`archive <name>`：dry-run 全驗 → 寫 note（可續跑）→ 併主 spec → 搬目錄。

順序與不變式：
1. `requires_authorization: true` 而沒給 `--authorized-by`：在任何讀寫與網路呼叫前拒絕
1b. tasks.md 有未勾選項目：拒絕（不動檔案、不碰服務）；`--allow-incomplete` 明確略過，
   未完成數記進 `incomplete_at_archive`，續跑時以此記錄放行
2. 本機全驗：metadata、格式、requirement_overlap、base 過時、狀態（被擋住拒絕）、
   delta 併回試算——任一失敗即停，服務一次都不碰
3. 先查完每條待寫 requirement 的鏈頭（同 topic 中 `superseded_by` 為空者）；
   鏈頭多於一則就停，此時尚未寫入任何 note
4. 逐則寫 note，每寫成一則立刻把 id 寫回 `.openspec.yaml` 的 `notes`；中途失敗
   change 留在原處，重跑跳過已寫的項目
5. 全部 note 寫完、`note_id` 回填後，才寫主 spec（`spec_applied: true`），最後搬目錄；
   每個 capability 寫入前先記 `spec_applying: {cap: sha256}`（write-ahead），寫完才移進
   `spec_applied_caps`，兩者之間中斷時續跑以雜湊認出已套用、不當成外部修改
6. 每則 note 寫入前先記它依據內容的雜湊（`note_digests`，write-ahead）：requirement
   是 delta 區塊正規化內容、總結是總結內文；續跑時已寫入（含從服務端採用）的項目
   雜湊與目前內容不符就拒絕——部分封存已開始，delta 不可再改。舊 metadata 沒有
   雜湊時維持原行為（doctor `tasks.archive_note_agreement` 給 warn）
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from lore_vault.binding import resolve_binding

from . import specs
from .vault_client import ServiceError, VaultClient
from .workspace import (
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    SUMMARY_KEY,
    Change,
    Workspace,
    atomic_write_text,
    derive_status,
    trial_merge,
    validate_change,
)

DEFAULT_AUTHOR = "lore-vault-tasks"
_MAX_SECTION = 4000
NOTE_DIGESTS_KEY = "note_digests"


class ArchiveError(Exception):
    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


@dataclass
class ArchiveResult:
    name: str
    destination: str
    note_id: str
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # 實際寫入的 vault（正式 key，也記在 metadata；快照推送依 metadata 分份）
    vault: str = ""


def _section(text: str, names: tuple[str, ...]) -> str:
    """取 Markdown `## <names>` 區段內文（不含標題）。"""
    lines = specs.normalize_text(text).split("\n")
    out: list[str] = []
    capturing = False
    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            title = m.group(1).strip().lower()
            capturing = any(n.lower() in title for n in names)
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()[:_MAX_SECTION]


def _read(change: Change, filename: str) -> str:
    path = change.path / filename
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def _goal(change: Change) -> str:
    goal = change.meta.get("goal")
    if isinstance(goal, str) and goal.strip():
        return goal.strip()
    why = _section(_read(change, "proposal.md"), ("Why",))
    first = why.split("\n", 1)[0].strip() if why else ""
    return first[:80] or change.name


def _requirement_items(change: Change) -> list[tuple[str, str, str, str]]:
    """(key, op, capability, block raw)：ADDED／MODIFIED 各一則 note。"""
    items = []
    for cap, plan in change.plans().items():
        for op, blocks in (
            (specs.OP_ADDED, plan.added),
            (specs.OP_MODIFIED, plan.modified),
        ):
            for block in blocks:
                items.append(
                    (specs.requirement_key(cap, block.name), op, cap, block.raw)
                )
    return items


def _removed_keys(change: Change) -> list[str]:
    return [
        specs.requirement_key(cap, name)
        for cap, plan in change.plans().items()
        for name in plan.removed
    ]


def _requirement_body(
    change: Change, op: str, raw: str, authorized_by: str | None
) -> str:
    proposal = _read(change, "proposal.md")
    design = _read(change, "design.md")
    parts = [f"change：{change.name}（{op}）", "", "## Requirement", "", raw.strip()]
    why = _section(proposal, ("Why",))
    if why:
        parts += ["", "## Why", "", why]
    tradeoff = _section(design, ("取捨", "Decision", "Trade"))
    if tradeoff:
        parts += ["", "## 取捨", "", tradeoff]
    if authorized_by:
        parts += ["", f"授權：{authorized_by}"]
    return "\n".join(parts)


def _summary_body(
    change: Change, req_notes: dict[str, str], authorized_by: str | None
) -> str:
    proposal = _read(change, "proposal.md")
    done, total = change.tasks_progress()
    parts = [f"change：{change.name}"]
    for heading, names in (
        ("Why", ("Why",)),
        ("What Changes", ("What Changes",)),
        ("Impact", ("Impact",)),
    ):
        text = _section(proposal, names)
        if text:
            parts += ["", f"## {heading}", "", text]
    parts += ["", "## 進度", "", f"tasks.md：{done}/{total} 完成"]
    incomplete = change.meta.get("incomplete_at_archive")
    if incomplete:
        parts.append(f"封存時未完成：{incomplete} 項（以 --allow-incomplete 略過）")
    if req_notes:
        parts += ["", "## Requirements", ""]
        parts += [f"- [[{specs.requirement_topic(k)}]]（{k}）" for k in req_notes]
    removed = _removed_keys(change)
    if removed:
        parts += ["", "## REMOVED", ""] + [f"- {k}" for k in removed]
    if authorized_by:
        parts += ["", f"授權：{authorized_by}"]
    return "\n".join(parts)


def _requirement_digests(change: Change) -> dict[str, str]:
    """每條待寫 requirement 的 delta 區塊正規化內容 sha256。"""
    return {
        key: specs.block_hash(raw) for key, _op, _cap, raw in _requirement_items(change)
    }


def _summary_digest(change: Change, notes: dict[str, str]) -> str:
    """總結 note 依據內容的 sha256：總結內文（不含授權行——續跑可不重帶
    `--authorized-by`）；requirement 集合變了時 `req_notes` 跟著變，雜湊也不同。"""
    req_notes = {k: notes.get(k, "") for k, *_ in _requirement_items(change)}
    body = _summary_body(change, req_notes, None)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _digest_mismatch(
    change: Change, key: str, notes: dict[str, str], current: dict[str, str]
) -> bool:
    """已記雜湊且與目前內容不符；沒記雜湊（舊 metadata）回 False，維持原行為。"""
    recorded = (change.meta.get(NOTE_DIGESTS_KEY) or {}).get(key)
    if recorded is None:
        return False
    if key == SUMMARY_KEY:
        return recorded != _summary_digest(change, notes)
    return recorded != current.get(key)


def _reject_changed(name: str, keys: list[str]) -> ArchiveError:
    return ArchiveError(
        f"{name} 的部分封存已開始，delta 不可再改：已寫入的 note 與目前內容不符",
        [
            f"內容已變更：{'、'.join(keys)}",
            "請還原 delta（及 proposal）到第一次 archive 時的內容，或人工處理"
            "已寫入的 note 與 metadata 後再重跑",
        ],
    )


def _check_written_digests(change: Change, notes: dict[str, str]) -> None:
    if not notes or not change.meta.get(NOTE_DIGESTS_KEY):
        return
    current = _requirement_digests(change)
    changed = [k for k in notes if _digest_mismatch(change, k, notes, current)]
    if changed:
        raise _reject_changed(change.name, changed)


def _record_digest(change: Change, key: str, digest: str) -> None:
    """write-ahead：寫 note 前先落地雜湊，HTTP 成功、回寫 notes 前中斷時，
    續跑採用服務端那則 note 也比得出內容是否被改過。"""
    digests = dict(change.meta.get(NOTE_DIGESTS_KEY) or {})
    digests[key] = digest
    change.meta[NOTE_DIGESTS_KEY] = digests
    change.save()


def _lookup_requirement(
    client: VaultClient, vault: str, space: str, key: str, change_name: str
) -> tuple[str | None, str | None]:
    """回傳 (本 change 已寫過的 note id, 鏈頭 id)。

    前一次 archive 若在 HTTP write 成功、本機回寫 metadata 前中斷，服務端會留一則
    同時帶 `change:<name>` 與 `req:` topic 的 note；採用它，不再重寫成孤兒。"""
    topic = specs.requirement_topic(key)
    items = client.list_topic(vault, space, topic)
    mine = [
        i["id"]
        for i in items
        if specs.change_topic(change_name) in (i.get("topics") or [])
    ]
    if len(mine) > 1:
        raise ArchiveError(
            f"{key}：服務端已有多則本 change 的 note，無法決定採用哪一則",
            [f"note：{', '.join(mine)}"],
        )
    if mine:
        return mine[0], None
    heads = [item["id"] for item in items if not item.get("superseded_by")]
    if len(heads) > 1:
        raise ArchiveError(
            f"{key}：同一 requirement 的鏈頭不只一則，無法決定 supersedes",
            [f"鏈頭：{', '.join(heads)}（請先以 update 修正 supersedes 鏈）"],
        )
    return None, (heads[0] if heads else None)


def _existing_summary(
    client: VaultClient, vault: str, space: str, change_name: str
) -> str | None:
    """本 change 已寫過的總結 note（帶 change topic、無 req: topic、總結標題）。"""
    prefix = f"變更 {change_name}："
    found = [
        i["id"]
        for i in client.list_topic(vault, space, specs.change_topic(change_name))
        if str(i.get("title", "")).startswith(prefix)
        and not any(str(t).startswith("req:") for t in i.get("topics") or [])
    ]
    if len(found) > 1:
        raise ArchiveError(
            f"服務端已有多則 {change_name} 的總結 note", [", ".join(found)]
        )
    return found[0] if found else None


def _resolve_vault(
    client: VaultClient, ws: Workspace, change: Change, vault: str | None, space: str
) -> str:
    key = vault or change.meta.get("vault")
    if not key:
        key = resolve_binding(ws.project_root).key
    return client.resolve_vault(str(key), space)


def archive_change(
    ws: Workspace,
    name: str,
    *,
    client_factory: Callable[[], VaultClient],
    authorized_by: str | None = None,
    allow_incomplete: bool = False,
    vault: str | None = None,
    author: str = DEFAULT_AUTHOR,
    now: _dt.datetime | None = None,
) -> ArchiveResult:
    change = ws.find_active(name)
    if change is None:
        raise ArchiveError(f"找不到 active change：{name}")
    meta = change.meta
    # 1. 授權閘門：可稽核的慣例性閘門，在任何寫入與網路呼叫之前
    if meta.get("requires_authorization") and not (authorized_by or "").strip():
        raise ArchiveError(
            f"{name} 標記 requires_authorization: true，須帶 --authorized-by <名字>"
        )
    authorized_by = (authorized_by or "").strip() or None
    # 1b. 未完成的 tasks：在任何寫入之前；續跑只認第一次留下的略過記錄
    done, total = change.tasks_progress()
    incomplete = total - done
    if incomplete > 0:
        if not allow_incomplete and "incomplete_at_archive" not in meta:
            raise ArchiveError(
                f"{name} 的 tasks.md 尚未完成（{done}/{total} 完成，"
                f"未完成 {incomplete} 項），不可封存",
                ["勾完 tasks.md，或確認要略過時帶 --allow-incomplete"],
            )
        # 只改記憶體中的 metadata，第一次 change.save() 時才落地
        meta["incomplete_at_archive"] = incomplete

    # 2. 本機全驗
    resumed_merge = bool(meta.get("spec_applied"))
    # 已寫回主 spec 的 capability（併主 spec 中途失敗時記錄）
    applied_caps: list[str] = list(meta.get("spec_applied_caps") or [])
    if not resumed_merge and _reconcile_applying(change, ws, applied_caps):
        change.save()
    # 續跑時只略過已套用的 capability；未套用的仍要驗 base 與 overlap，
    # 否則中斷期間被外部改過的主 spec 會被過時 delta 覆蓋
    if not resumed_merge:
        errors = validate_change(change, ws, ws.active(), skip=applied_caps)
        if errors:
            raise ArchiveError(f"{name} 未通過 validate", errors)
    status, reasons = derive_status(change, ws)
    if status in (STATUS_BLOCKED, STATUS_UNKNOWN):
        raise ArchiveError(f"{name} 狀態為「{status}」，不可封存", reasons)
    merged: dict[str, str] = {}
    if not resumed_merge and not meta.get("skip_specs"):
        merged, merge_errors = trial_merge(change, ws, skip=applied_caps)
        if merge_errors:
            raise ArchiveError(f"{name} delta 併回試算失敗", merge_errors)
    # 已寫入的 note 與目前內容須一致（服務呼叫之前；主 spec 已併的續跑也查）
    _check_written_digests(change, dict(meta.get("notes") or {}))

    # 3. 服務：vault、鏈頭
    space = str(meta.get("space") or "dev")
    notes: dict[str, str] = dict(meta.get("notes") or {})
    result = ArchiveResult(name=name, destination="", note_id="")
    already = set(notes)
    try:
        client = client_factory()
        vault_key = _resolve_vault(client, ws, change, vault, space)
        if meta.get("vault") not in (None, vault_key):
            raise ArchiveError(
                f"vault 與先前記錄不同：{meta.get('vault')} → {vault_key}"
            )
        meta["vault"] = vault_key
        result.vault = vault_key
        pending = [it for it in _requirement_items(change) if it[0] not in notes]
        current = _requirement_digests(change)
        heads: dict[str, str | None] = {}
        for key, *_ in list(pending):
            existing, heads[key] = _lookup_requirement(
                client, vault_key, space, key, name
            )
            if existing:
                if _digest_mismatch(change, key, notes, current):
                    raise _reject_changed(name, [key])
                notes[key] = existing
                meta["notes"] = dict(notes)
                change.save()
                result.skipped.append(key)
        pending = [it for it in pending if it[0] not in notes]

        # 4. 逐則寫入，每則立即回寫 metadata
        for key, op, _cap, raw in pending:
            _record_digest(change, key, current[key])
            note_id = client.write(
                vault_key,
                space,
                title=f"req:{key}",
                body=_requirement_body(change, op, raw, authorized_by),
                topics=[specs.change_topic(name), specs.requirement_topic(key)],
                supersedes=heads[key],
                author=author,
            )
            notes[key] = note_id
            meta["notes"] = dict(notes)
            change.save()
            result.written.append(key)
        result.skipped += [k for k, *_ in _requirement_items(change) if k in already]
        if SUMMARY_KEY not in notes:
            existing = _existing_summary(client, vault_key, space, name)
            if existing:
                if _digest_mismatch(change, SUMMARY_KEY, notes, current):
                    raise _reject_changed(name, [SUMMARY_KEY])
                notes[SUMMARY_KEY] = existing
                meta["notes"] = dict(notes)
        if SUMMARY_KEY not in notes:
            req_notes = {k: notes[k] for k, *_ in _requirement_items(change)}
            _record_digest(change, SUMMARY_KEY, _summary_digest(change, notes))
            summary_id = client.write(
                vault_key,
                space,
                title=f"變更 {name}：{_goal(change)}",
                body=_summary_body(change, req_notes, authorized_by),
                topics=[specs.change_topic(name)],
                links=list(req_notes.values()),
                author=author,
            )
            notes[SUMMARY_KEY] = summary_id
            meta["notes"] = dict(notes)
            result.written.append(SUMMARY_KEY)
        else:
            result.skipped.append(SUMMARY_KEY)
        meta["note_id"] = notes[SUMMARY_KEY]
        if authorized_by:
            meta["authorized_by"] = authorized_by
        change.save()
    except ServiceError as exc:
        change.save()
        raise ArchiveError(
            "Lore Vault 寫入失敗，change 留在原處（重跑會跳過已寫的 note）："
            + exc.detail,
            [f"已寫入：{', '.join(notes) or '無'}"],
        ) from None

    # 5. 併主 spec → 搬目錄
    if not resumed_merge:
        # 每寫完一個 capability 就記下，中途失敗重跑只補未寫的
        # write-ahead：寫之前先記下即將寫入內容的雜湊，寫完與 spec_applied_caps
        # 之間中斷時，續跑才認得出主 spec 已是這次的結果（見 _reconcile_applying）
        for cap, text in merged.items():
            meta[SPEC_APPLYING_KEY] = {cap: _digest(text.encode("utf-8"))}
            change.save()
            atomic_write_text(ws.main_spec_path(cap), text)
            applied_caps.append(cap)
            meta["spec_applied_caps"] = list(applied_caps)
            meta.pop(SPEC_APPLYING_KEY, None)
            change.save()
        meta["spec_applied"] = True
        change.save()
    stamp = (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)
    meta["archived_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    change.save()
    ws.archive_dir.mkdir(parents=True, exist_ok=True)
    destination = ws.archive_dir / f"{stamp.strftime('%Y-%m-%d')}-{name}"
    if destination.exists():
        raise ArchiveError(f"封存目錄已存在：{destination.name}")
    change.path.rename(destination)
    result.destination = str(destination)
    result.note_id = str(meta["note_id"])
    return result


SPEC_APPLYING_KEY = "spec_applying"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reconcile_applying(change: Change, ws: Workspace, applied_caps: list[str]) -> bool:
    """續跑：`spec_applying` 記的 capability 若主 spec 位元組雜湊等於記錄值，代表上次
    已寫入、只差沒記進 `spec_applied_caps`——補記並清掉 `spec_applying`，回傳 True。

    雜湊不同（尚未寫入，或中斷後被外部改過）就不動 metadata，交給後面的
    base／試算驗證照常處理。雜湊對的是實際落地的位元組：`atomic_write_text`
    以 `newline=""` 寫入、行尾已由 `trial_merge` 決定，
    故比對 `text.encode("utf-8")`。"""
    applying = change.meta.get(SPEC_APPLYING_KEY)
    if not isinstance(applying, dict) or not applying:
        return False
    done = []
    for cap, digest in applying.items():
        path = ws.main_spec_path(str(cap))
        if path.is_file() and _digest(path.read_bytes()) == digest:
            done.append(str(cap))
    if len(done) != len(applying):
        return False
    for cap in done:
        if cap not in applied_caps:
            applied_caps.append(cap)
    change.meta["spec_applied_caps"] = list(applied_caps)
    change.meta.pop(SPEC_APPLYING_KEY, None)
    return True


def describe_result(result: ArchiveResult) -> list[str]:
    lines = [
        f"已封存 {result.name} → {result.destination}",
        f"note_id：{result.note_id}",
    ]
    if result.written:
        lines.append("本次寫入：" + "、".join(result.written))
    if result.skipped:
        lines.append("先前已寫（跳過）：" + "、".join(result.skipped))
    return lines
