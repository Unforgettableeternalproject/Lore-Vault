"""任務快照：本機任務目錄的推導結果推到服務端側載（`/v1/blob_put`），給 UI 讀。

快照 schema（v1，與前端 `ui/app` 共用的契約；key `tasks-snapshot`、mime
`application/json`）：

```
{"schema": 1,
 "changes": [{"name", "status", "reasons": [str],
              "blocked_by": [{"id", "resolved": true|false|null}],
              "depends_on": [{"name", "archived": bool}],
              "requires_authorization": bool,
              "tasks": {"done", "total"}, "source": str|null, "why": str|null,
              "specs": [{"capability", "requirement", "op"}],
              "note_id": str|null, "archived_at": str|null}]}
```

- 語料邊界：只含推導結果與標題。spec delta 全文、tasks.md 逐項文字、design.md
  一律不含；`why` 是 proposal 的「Why」段（本來就會進 archive note），
  截至 `MAX_WHY_CHARS`
- 內容是確定性的（不含產生時間、鍵排序固定）：同一份本機狀態永遠得到同一組位元組，
  doctor `tasks.snapshot_sync` 以雜湊比對；同步時間由服務端側載的 `updated` 提供
- 超過服務端上限（64KB）時先拿掉已封存 change 的 `why`，仍超過就拒絕推送（不截斷）
- 依 vault 分份：每個 vault 只收 metadata `vault` 解析後等於它的 change，加上沒記
  vault 的 change（歸到預設 vault：明確指定或專案目錄 binding）。一次推送對所有涉及的
  vault 各推一份——change 封存到別的 vault 後，原 vault 的快照同時更新、把它移除

## 計算來源（MCP-T6）

- vault 已有 `task-index`（任務層已改由服務端為權威）：**一律由服務端內容計算**
  （`remote_snapshot_bytes`：`task-index` 列舉＋各 `task-change:<name>`），
  不看本機目錄——MCP 與 CLI 推的是同一份計算結果，不會互相覆蓋。
  CLI 各指令結尾與 `sync`、MCP 的 propose／edit／validate record_base／archive／
  sync_specs 成功後都走這條（`push_remote`）
- vault 沒有 `task-index`（還沒用過服務端任務層的舊專案）：照舊由本機目錄計算
  （`push`）；服務不可達時 CLI 只警告
- 服務端計算的每筆 change 多一個 `state`（`active`／`pending_apply`／`archived`；
  UI 可忽略未知欄位，schema 仍是 v1）。`pending_apply` 的 status 為
  「已封存（待落地）」、帶 note_id／archived_at；`archived` 為「已完成」。
  另多一個 `approved`（`{"by", "at"}` 或 null）：進行中的需授權 change 讀 UI 核准紀錄
  （`remote_ops.authorization_states`，內容雜湊相符才算），核准有效時 status 依其餘條件
  推導（通常是「可開工」）並在 reasons 附「已由 … 核准（…）」；沒有核准或已過期維持
  「待授權」，原因指向 UI 任務頁。本機目錄計算的快照沒有這個欄位（離線模式仍是
  `--authorized-by` 的文字）。核准只寫授權紀錄、不會觸發推送：要等下一次推送才進快照。
  索引標 `archived` 但沒有 change 文件的（遷移的舊封存）只有名稱與狀態
- DECISIONS 判定：本機有 DECISIONS.md 以本機為準，否則用服務端鏡像
  `task-decisions`，都沒有為 None（「無法判定」）
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lore_vault.api import task_format as tf
from lore_vault.binding import resolve_binding

from . import remote_ops, specs
from . import remote_store as rs
from .archive import _read, _section
from .vault_client import VaultClient
from .workspace import (
    SPACE_DEV,
    STATUS_DONE,
    STATUS_PENDING_APPLY,
    Change,
    Workspace,
    derive_status,
)

SNAPSHOT_KEY = tf.SNAPSHOT_KEY
SNAPSHOT_MIME = "application/json"
SNAPSHOT_SCHEMA = 1
MAX_BYTES = 64 * 1024
MAX_WHY_CHARS = 1000

CHANGE_FIELDS = frozenset(
    {
        "name",
        "status",
        "reasons",
        "blocked_by",
        "depends_on",
        "requires_authorization",
        "tasks",
        "source",
        "why",
        "specs",
        "note_id",
        "archived_at",
    }
)


class SnapshotTooLarge(Exception):
    """快照超過服務端側載上限，拿掉已封存 change 的 why 後仍超過。"""


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _why(change: Change) -> str | None:
    text = _section(_read(change, "proposal.md"), ("Why",))
    return text[:MAX_WHY_CHARS] or None


def _specs(change: Change) -> list[dict[str, str]]:
    try:
        plans = change.plans()
    except (OSError, ValueError):
        return []
    return [
        {"capability": cap, "requirement": name, "op": op}
        for cap, plan in plans.items()
        for op, name in plan.operations()
    ]


def _change_entry(
    change: Change,
    ws: Workspace,
    archived_names: set[str],
    decisions: dict[str, bool] | None,
) -> dict[str, Any]:
    status, reasons = derive_status(change, ws)
    done, total = change.tasks_progress()
    meta = change.meta
    blocked = [d for d in meta.get("blocked_by") or [] if isinstance(d, str)]
    depends = [d for d in meta.get("depends_on") or [] if isinstance(d, str)]
    return {
        "name": change.name,
        "status": status,
        "reasons": list(reasons),
        "blocked_by": [
            {
                "id": d,
                "resolved": (
                    decisions[d] if decisions is not None and d in decisions else None
                ),
            }
            for d in blocked
        ],
        "depends_on": [{"name": d, "archived": d in archived_names} for d in depends],
        "requires_authorization": bool(meta.get("requires_authorization")),
        "tasks": {"done": done, "total": total},
        "source": _str_or_none(meta.get("source")),
        "why": _why(change),
        "specs": _specs(change),
        "note_id": _str_or_none(meta.get("note_id")),
        "archived_at": change.archived_at() if change.archived else None,
    }


def build_snapshot(
    ws: Workspace, include: Callable[[Change], bool] | None = None
) -> dict[str, Any]:
    """`include`：只放通過的 change（依 vault 分份用）；None 為全部。"""
    archived = ws.archived()
    archived_names = {c.name for c in archived}
    decisions = ws.decisions()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "changes": [
            _change_entry(c, ws, archived_names, decisions)
            for c in ws.active() + archived
            if include is None or include(c)
        ],
    }


def encode(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def snapshot_bytes(
    ws: Workspace,
    *,
    max_bytes: int = MAX_BYTES,
    include: Callable[[Change], bool] | None = None,
) -> bytes:
    return _fit(build_snapshot(ws, include), max_bytes)


def _fit(snapshot: dict[str, Any], max_bytes: int) -> bytes:
    """超過上限時先拿掉已封存 change 的 why，仍超過就拒絕（不截斷）。"""
    data = encode(snapshot)
    if len(data) <= max_bytes:
        return data
    for entry in snapshot["changes"]:
        if entry["archived_at"] is not None:
            entry["why"] = None
    data = encode(snapshot)
    if len(data) > max_bytes:
        raise SnapshotTooLarge(
            f"任務快照 {len(data)} 位元組，超過服務端上限 {max_bytes} 位元組"
        )
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_vault(client: VaultClient, ws: Workspace, vault: str | None) -> str:
    """與 archive 同一條路徑：明確指定 > 專案目錄 binding，再經服務解析成正式 key。"""
    key = vault or resolve_binding(ws.project_root).key
    return client.resolve_vault(str(key), SPACE_DEV)


def vault_payloads(
    ws: Workspace, client: VaultClient, vault: str | None = None
) -> dict[str, bytes]:
    """{目的 vault: 該 vault 的快照位元組}，預設 vault 排第一。

    預設 vault（`vault` 或專案目錄 binding）一定在內，沒記 vault 的 change 歸它；
    其餘為各 change metadata `vault` 經服務解析後的正式 key（可能是別名，逐一解析）。"""
    default = resolve_vault(client, ws, vault)
    resolved: dict[str, str] = {}
    owners: dict[Path, str] = {}
    for change in ws.active() + ws.archived():
        key = change.meta.get("vault")
        if isinstance(key, str) and key.strip():
            if key not in resolved:
                resolved[key] = client.resolve_vault(key, SPACE_DEV)
            owners[change.path] = resolved[key]
        else:
            owners[change.path] = default
    targets = [default, *sorted(set(owners.values()) - {default})]
    return {
        target: snapshot_bytes(
            ws, include=lambda c, t=target: owners.get(c.path, default) == t
        )
        for target in targets
    }


@dataclass(frozen=True)
class PushResult:
    vault: str
    updated: str
    size_bytes: int
    changes: int


def push(
    ws: Workspace, client: VaultClient, *, vault: str | None = None
) -> list[PushResult]:
    """對每個涉及的 vault 整份覆寫它的側載（見 `vault_payloads`）。失敗拋
    `ServiceError`／`SnapshotTooLarge`，由呼叫端決定要中止還是只警告；
    所有分份都先算完才開始推，超過上限時一份都不推。"""
    results = []
    for vault_key, data in vault_payloads(ws, client, vault).items():
        updated = client.put_blob(
            vault_key, SPACE_DEV, SNAPSHOT_KEY, data, mime=SNAPSHOT_MIME
        )
        changes = len(json.loads(data)["changes"])
        results.append(PushResult(vault_key, updated, len(data), changes))
    return results


# ── 服務端計算（MCP-T6）─────────────────────────────────────────────


def _remote_entry(
    change: rs.RemoteChange,
    rws: rs.RemoteWorkspace,
    archived_names: set[str],
    auth: remote_ops.AuthState | None,
) -> dict[str, Any]:
    entry = _change_entry(change, rws, archived_names, rws.decisions())
    entry["state"] = change.state
    entry["status"], entry["reasons"] = remote_ops.apply_authorization(
        entry["status"], entry["reasons"], auth
    )
    entry["approved"] = remote_ops.approved_info(auth)
    if change.state != rs.STATE_ACTIVE:
        apply = change.doc.get("apply") or {}
        stamp = change.meta.get("archived_at") or apply.get("archived_at")
        entry["archived_at"] = _str_or_none(stamp)
        entry["reasons"] = []
        entry["status"] = (
            STATUS_PENDING_APPLY
            if change.state == rs.STATE_PENDING_APPLY
            else STATUS_DONE
        )
    return entry


def _index_only_entry(name: str) -> dict[str, Any]:
    """索引標 `archived` 但沒有 change 文件（遷移的舊封存）：只知道名稱與狀態。"""
    return {
        "name": name,
        "status": STATUS_DONE,
        "reasons": [],
        "blocked_by": [],
        "depends_on": [],
        "requires_authorization": False,
        "tasks": {"done": 0, "total": 0},
        "source": None,
        "why": None,
        "specs": [],
        "note_id": None,
        "archived_at": None,
        "state": rs.STATE_ARCHIVED,
        "approved": None,
    }


def build_remote_snapshot(
    changes: list[rs.RemoteChange],
    archived_names: set[str],
    decisions: dict[str, bool] | None,
    auth: dict[str, remote_ops.AuthState] | None = None,
) -> dict[str, Any]:
    """`changes`：`list_changes(include_archived=True)` 的結果（含已落地的文件）。
    `auth`：`remote_ops.authorization_states` 的結果（沒列到的 change 視為未核准）。
    順序：進行中（active／pending_apply，依名稱）在前，已落地依封存時間。"""
    rws = rs.RemoteWorkspace(
        root=Path("remote"),
        changes=changes,
        archived_names=archived_names,
        decisions_map=decisions,
    )
    live = [c for c in changes if c.state != rs.STATE_ARCHIVED]
    done = [c for c in changes if c.state == rs.STATE_ARCHIVED]
    states = auth or {}
    entries = [_remote_entry(c, rws, archived_names, states.get(c.name)) for c in live]
    finished = [_remote_entry(c, rws, archived_names, None) for c in done]
    have = {c.name for c in changes}
    finished += [_index_only_entry(n) for n in sorted(archived_names - have)]
    finished.sort(key=lambda e: (e["archived_at"] or "", e["name"]))
    return {"schema": SNAPSHOT_SCHEMA, "changes": entries + finished}


async def remote_snapshot_bytes(
    store: rs.RemoteStore,
    local: Workspace | None = None,
    *,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """服務端內容（`task-index`＋`task-change:*`）算出的快照位元組；
    `local`：stdio 的本機工作區（只用來讀 DECISIONS.md，其餘不看本機）。"""
    changes, archived = await store.list_changes(include_archived=True)
    decisions = None
    if local is not None:
        decisions = local.decisions()
    if decisions is None and any(c.meta.get("blocked_by") for c in changes):
        decisions = await rs.resolve_decisions(store, None)
    auth = await remote_ops.authorization_states(store, changes)
    return _fit(build_remote_snapshot(changes, archived, decisions, auth), max_bytes)


async def has_remote_index(store: rs.RemoteStore) -> bool:
    return await store.get_blob(rs.INDEX_KEY) is not None


async def push_remote(
    store: rs.RemoteStore, local: Workspace | None = None
) -> PushResult:
    """以服務端內容計算並整份覆寫該 vault 的 `tasks-snapshot`。失敗拋
    `remote_store` 的例外或 `SnapshotTooLarge`，由呼叫端決定要不要只警告。"""
    data = await remote_snapshot_bytes(store, local)
    response = await store.put_derived(SNAPSHOT_KEY, data)
    changes = len(json.loads(data)["changes"])
    return PushResult(
        store.vault, str(response.get("updated") or ""), len(data), changes
    )


def shape_errors(data: bytes) -> list[str]:
    """服務端內容是否符合快照 schema v1（給 doctor `tasks.snapshot_shape`）。"""
    try:
        snapshot = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ["內容不是 UTF-8 JSON"]
    if not isinstance(snapshot, dict):
        return ["頂層必須是物件"]
    errors: list[str] = []
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        errors.append(
            f"schema 必須是 {SNAPSHOT_SCHEMA}，得到 {snapshot.get('schema')!r}"
        )
    changes = snapshot.get("changes")
    if not isinstance(changes, list):
        return [*errors, "changes 必須是陣列"]
    for i, entry in enumerate(changes):
        if not isinstance(entry, dict):
            errors.append(f"changes[{i}] 必須是物件")
            continue
        missing = sorted(CHANGE_FIELDS - set(entry))
        if missing:
            errors.append(f"changes[{i}] 缺欄位：{', '.join(missing)}")
            continue
        if not isinstance(entry["name"], str) or not isinstance(entry["status"], str):
            errors.append(f"changes[{i}] 的 name／status 必須是字串")
        tasks = entry["tasks"]
        if not (
            isinstance(tasks, dict)
            and isinstance(tasks.get("done"), int)
            and isinstance(tasks.get("total"), int)
        ):
            errors.append(f"changes[{i}].tasks 必須是 {{done, total}} 整數")
        for key in ("reasons", "blocked_by", "depends_on", "specs"):
            if not isinstance(entry[key], list):
                errors.append(f"changes[{i}].{key} 必須是陣列")
        for op in entry["specs"] if isinstance(entry["specs"], list) else []:
            if not isinstance(op, dict) or op.get("op") not in (
                specs.OP_ADDED,
                specs.OP_MODIFIED,
                specs.OP_REMOVED,
            ):
                errors.append(
                    f"changes[{i}].specs 的 op 必須是 ADDED／MODIFIED／REMOVED"
                )
                break
    return errors
