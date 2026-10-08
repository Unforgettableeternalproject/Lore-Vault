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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lore_vault.binding import resolve_binding

from . import specs
from .archive import _read, _section
from .vault_client import VaultClient
from .workspace import SPACE_DEV, Change, Workspace, derive_status

SNAPSHOT_KEY = "tasks-snapshot"
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
    snapshot = build_snapshot(ws, include)
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
