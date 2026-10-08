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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lore_vault.binding import resolve_binding

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


def build_snapshot(ws: Workspace) -> dict[str, Any]:
    archived = ws.archived()
    archived_names = {c.name for c in archived}
    decisions = ws.decisions()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "changes": [
            _change_entry(c, ws, archived_names, decisions)
            for c in ws.active() + archived
        ],
    }


def encode(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def snapshot_bytes(ws: Workspace, *, max_bytes: int = MAX_BYTES) -> bytes:
    snapshot = build_snapshot(ws)
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


@dataclass(frozen=True)
class PushResult:
    vault: str
    updated: str
    size_bytes: int
    changes: int


def push(ws: Workspace, client: VaultClient, *, vault: str | None = None) -> PushResult:
    """整份覆寫服務端側載。失敗拋 `ServiceError`／`SnapshotTooLarge`，由呼叫端決定
    要中止還是只警告。"""
    data = snapshot_bytes(ws)
    vault_key = resolve_vault(client, ws, vault)
    updated = client.put_blob(
        vault_key, SPACE_DEV, SNAPSHOT_KEY, data, mime=SNAPSHOT_MIME
    )
    changes = len(json.loads(data)["changes"])
    return PushResult(vault_key, updated, len(data), changes)
