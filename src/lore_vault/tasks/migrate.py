"""既有 `openspec/` 遷移到服務端版本化側載（TASK_LAYER_MCP §5.1、MCP-T7）。

`python -m lore_vault.tasks migrate [--vault KEY] [--dry-run] [--json]`：

1. 確保 `task-index` 存在
2. 每個本機 active change：以 `expected_version=0` 建成 `task-change:<name>`
   （`RemoteStore.create_change`，內容為 `remote_store.local_doc`），補進索引，
   再在本機 `.openspec.yaml` 回填 `remote_version`／`remote_digest`
3. 每個本機已封存的 change：推一份 `state: archived` 的 change 文件（meta 帶
   note_id／archived_at 等、proposal 保留 why，見 `archived_doc`）並以 `archived`
   登記進索引（供 `depends_on` 判定）；不重寫 note。記錄的 vault 不是目標 vault 的
   略過
4. 主 spec 鏡像：本機 `specs/<cap>/spec.md`（與 active change 的新 capability）
   還沒有鏡像的，以 `expected_version=0` 建立
5. 推送 `task-decisions`（`remote_ops.push_decisions`）與以服務端內容重算的任務快照
   （`snapshot.push_remote`）
6. 全部成功（沒有衝突或錯誤）才在本機 `config.yaml` 寫 `remote: true`
   （`workspace.enable_remote`，同 `init --remote`）

可重跑，且一律不覆寫服務端既有內容：
- 本機已有 `remote_version` → 已遷移，略過
- 服務端已有同名 change、本機沒有 `remote_version`：內容雜湊相同 → 只回填本機
  同步欄位（上次跑到一半）；不同 → 衝突，回報後不動
- 封存 change：服務端已有 archived 文件且 note_id 或內容相同 → 略過（只補索引）；
  狀態或內容不同、或索引狀態不是 `archived` → 衝突（`pending_apply` 例外：那是
  MCP 段一封存、等 sync_specs 落地，略過）
- 鏡像已存在但與本機不同 → 衝突（正常推送走 stdio validate／init，不在這裡覆寫）

有任何衝突或錯誤 exit 1。`--dry-run` 只讀服務端、列出會做的事，不寫 config.yaml。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import remote_ops, snapshot
from . import remote_store as rs
from .vault_client import ServiceError, VaultClient
from .workspace import Change, Workspace, enable_remote

if TYPE_CHECKING:
    from .cli import _Env

CREATED = "created"
BACKFILLED = "backfilled"
INDEXED = "indexed"
SKIPPED = "skipped"
CONFLICT = "conflict"
ERROR = "error"
PLANNED = "planned"
FAILED = (CONFLICT, ERROR)

_LABELS = {
    CREATED: "已建立",
    BACKFILLED: "已回填同步欄位",
    INDEXED: "已登記",
    SKIPPED: "略過",
    CONFLICT: "衝突",
    ERROR: "錯誤",
    PLANNED: "將處理（dry-run）",
}


@dataclass
class Item:
    action: str
    detail: str = ""
    version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action}
        if self.detail:
            data["detail"] = self.detail
        if self.version is not None:
            data["version"] = self.version
        return data


@dataclass
class MigrateReport:
    vault: str
    dry_run: bool
    index_created: bool = False
    changes: dict[str, Item] = field(default_factory=dict)
    archived: dict[str, Item] = field(default_factory=dict)
    mirrors: dict[str, Item] = field(default_factory=dict)
    # decisions／snapshot：遷移結尾的衍生推送
    extras: dict[str, Item] = field(default_factory=dict)
    # config.yaml `remote: true`：enabled（本次寫入）／already／None（未寫）
    remote_mode: str | None = None

    def _groups(self) -> tuple[dict[str, Item], ...]:
        return (self.changes, self.archived, self.mirrors, self.extras)

    @property
    def ok(self) -> bool:
        return not any(
            item.action in FAILED for group in self._groups() for item in group.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault": self.vault,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "index_created": self.index_created,
            "changes": {k: v.to_dict() for k, v in self.changes.items()},
            "archived": {k: v.to_dict() for k, v in self.archived.items()},
            "mirrors": {k: v.to_dict() for k, v in self.mirrors.items()},
            "decisions": self._extra("decisions"),
            "snapshot": self._extra("snapshot"),
            "remote_mode": self.remote_mode,
        }

    def _extra(self, key: str) -> dict[str, Any] | None:
        item = self.extras.get(key)
        return item.to_dict() if item else None

    def lines(self) -> list[str]:
        head = f"遷移到 {self.vault}"
        if self.dry_run:
            head += "（dry-run：只讀服務端，未寫入任何東西）"
        out = [head]
        if self.index_created:
            out.append("任務索引：已建立")
        for title, group in (
            ("change", self.changes),
            ("封存", self.archived),
            ("鏡像", self.mirrors),
            ("推送", self.extras),
        ):
            for name, item in group.items():
                version = f" v{item.version}" if item.version is not None else ""
                detail = f"（{item.detail}）" if item.detail else ""
                out.append(f"{title} {name}：{_LABELS[item.action]}{version}{detail}")
        counts = [
            f"{_LABELS[a]} {n}"
            for a in (CREATED, BACKFILLED, INDEXED, PLANNED, SKIPPED, CONFLICT, ERROR)
            if (n := self._count(a))
        ]
        out.append("合計：" + ("、".join(counts) if counts else "無事可做"))
        if self.remote_mode == "enabled":
            out.append("config.yaml：已寫入 remote: true（之後 CLI 以服務端為準）")
        elif self.remote_mode is None and not self.dry_run:
            out.append("config.yaml：有衝突或錯誤，未寫入 remote: true")
        return out

    def _count(self, action: str) -> int:
        return sum(
            item.action == action for group in self._groups() for item in group.values()
        )


def _remote_failure(exc: rs.RemoteError) -> Item:
    return Item(ERROR, f"服務拒絕（{exc.code or exc.status}）：{exc.message}")


def _backfill(change: Change, version: int, digest: str) -> None:
    change.meta[rs.REMOTE_VERSION_KEY] = version
    change.meta[rs.REMOTE_DIGEST_KEY] = digest
    change.save()


async def _migrate_change(
    store: rs.RemoteStore,
    change: Change,
    indexed: dict[str, str],
    *,
    dry_run: bool,
) -> Item:
    name = change.name
    if change.meta_error:
        return Item(ERROR, f"本機 .openspec.yaml 無法解析：{change.meta_error}")
    try:
        rs.check_name(name)
        remote = await store.get_change(name)
    except rs.StoreError as exc:
        return Item(ERROR, exc.message)
    doc = rs.local_doc(change)
    digest = rs.content_digest(doc)
    synced = rs.REMOTE_VERSION_KEY in change.meta
    if remote is not None:
        if synced:
            return Item(SKIPPED, "已遷移", remote.version)
        if remote.state != rs.STATE_ACTIVE:
            return Item(SKIPPED, f"服務端為 {remote.state}", remote.version)
        if remote.digest() != digest:
            return Item(
                CONFLICT,
                "服務端已有內容不同的同名 change，未覆寫"
                "（確認哪邊正確後用 MCP pull 或 edit 處理）",
                remote.version,
            )
        if dry_run:
            return Item(PLANNED, "服務端內容相同，回填本機同步欄位", remote.version)
        if name not in indexed:
            await store.set_index_state(name, remote.state)
            indexed[name] = remote.state
        _backfill(change, remote.version, digest)
        return Item(BACKFILLED, "服務端內容相同", remote.version)
    if synced:
        return Item(
            CONFLICT,
            f"本機記錄已同步過 v{change.meta.get(rs.REMOTE_VERSION_KEY)}，"
            "服務端卻沒有；未重建（確認後移除本機 remote_version 再重跑）",
        )
    if dry_run:
        return Item(PLANNED, "建立 task-change")
    try:
        created = await store.create_change(doc)
    except rs.StoreError as exc:
        if exc.code == "change_exists":
            return Item(CONFLICT, "建立時服務端已出現同名 change，重跑以比對內容")
        return Item(ERROR, exc.message)
    indexed[name] = rs.STATE_ACTIVE
    _backfill(change, created.version, digest)
    return Item(CREATED, "", created.version)


def archived_doc(change: Change) -> dict[str, Any]:
    """本機封存目錄 → 服務端最小 change 文件（`state: archived`）：meta 帶 note_id、
    archived_at、goal／source 等，proposal 保留 why，delta／tasks 照本機。
    服務端算的快照因此不會丟掉 note 連結與 why。"""
    doc = rs.local_doc(change)
    doc["state"] = rs.STATE_ARCHIVED
    doc["meta"].setdefault("archived_at", change.archived_at())
    return doc


async def _owner(
    store: rs.RemoteStore, change: Change, cache: dict[str, str]
) -> str | None:
    """封存 change 記錄的 vault（經服務解析成正式 key）；沒記回 None。"""
    key = change.meta.get("vault")
    if not isinstance(key, str) or not key.strip():
        return None
    if key not in cache:
        cache[key] = await store.resolve_vault(key)
    return cache[key]


async def _migrate_archived(
    store: rs.RemoteStore,
    change: Change,
    indexed: dict[str, str],
    vaults: dict[str, str],
    *,
    dry_run: bool,
) -> Item:
    try:
        name = rs.check_name(change.name)
    except rs.StoreError as exc:
        return Item(ERROR, exc.message)
    if change.meta_error:
        return Item(ERROR, f"本機 .openspec.yaml 無法解析：{change.meta_error}")
    try:
        owner = await _owner(store, change, vaults)
    except rs.RemoteError as exc:
        return Item(SKIPPED, f"記錄的 vault 無法解析（{exc.message}），未遷移")
    if owner is not None and owner != store.vault:
        return Item(SKIPPED, f"封存於 vault {owner}，不屬於 {store.vault}")
    state = indexed.get(name)
    if state == rs.STATE_PENDING_APPLY:
        return Item(SKIPPED, "服務端為 pending_apply（等 sync_specs 落地）")
    if state not in (None, rs.STATE_ARCHIVED):
        return Item(CONFLICT, f"索引中狀態為 {state}，本機卻已封存；未改動")
    doc = archived_doc(change)
    try:
        remote = await store.get_change(name)
    except rs.StoreError as exc:
        return Item(ERROR, exc.message)
    if remote is not None:
        same_note = change.meta.get("note_id") and remote.meta.get(
            "note_id"
        ) == change.meta.get("note_id")
        if remote.state != rs.STATE_ARCHIVED or not (
            same_note or remote.digest() == rs.content_digest(doc)
        ):
            return Item(
                CONFLICT,
                f"服務端已有內容不同的同名 change（{remote.state}），未覆寫",
                remote.version,
            )
        if state is None and not dry_run:
            await store.set_index_state(name, rs.STATE_ARCHIVED)
            indexed[name] = rs.STATE_ARCHIVED
        return Item(SKIPPED, "已遷移", remote.version)
    if dry_run:
        return Item(PLANNED, "建立 archived change 文件並登記進索引")
    try:
        created = await store.create_change(doc)
    except rs.StoreError as exc:
        if exc.code == "change_exists":
            return Item(CONFLICT, "建立時服務端已出現同名 change，重跑以比對內容")
        return Item(ERROR, exc.message)
    indexed[name] = rs.STATE_ARCHIVED
    return Item(INDEXED, "archived", created.version)


async def _pending_caps(store: rs.RemoteStore, indexed: dict[str, str]) -> set[str]:
    """`pending_apply` change 正在合併的 capability（鏡像領先 git，不比也不推）。"""
    caps: set[str] = set()
    for name, state in indexed.items():
        if state == rs.STATE_ARCHIVED:
            continue
        try:
            change = await store.get_change(name)
        except rs.StoreError:
            continue
        if change is not None and change.state == rs.STATE_PENDING_APPLY:
            caps |= set((change.doc.get("apply") or {}).get("merged_specs") or {})
    return caps


async def _migrate_mirror(
    store: rs.RemoteStore, cap: str, local: dict[str, str], *, dry_run: bool
) -> Item:
    exists = cap in local
    text = local.get(cap)
    try:
        mirror = await store.get_mirror(cap)
    except rs.StoreError as exc:
        return Item(ERROR, exc.message)
    if mirror is not None:
        if mirror.same_content(exists, text):
            return Item(SKIPPED, "與本機一致", mirror.version)
        return Item(
            CONFLICT,
            f"鏡像（{mirror.source}）與本機 specs/ 不同，未覆寫"
            "（確認後在 stdio 執行 validate 推送）",
            mirror.version,
        )
    if dry_run:
        return Item(PLANNED, "建立鏡像" if exists else "建立鏡像（新 capability）")
    try:
        version = await store.put_mirror(
            cap,
            exists=exists,
            text=text,
            source=rs.MIRROR_SOURCE_STDIO,
            expected_version=0,
        )
    except rs.RemoteError as exc:
        if exc.code == "version_conflict":
            return Item(CONFLICT, "建立時服務端已出現鏡像，重跑以比對內容")
        return _remote_failure(exc)
    return Item(CREATED, "" if exists else "新 capability", version)


async def migrate(
    ws: Workspace, store: rs.RemoteStore, *, dry_run: bool = False
) -> MigrateReport:
    report = MigrateReport(store.vault, dry_run)
    if not dry_run:
        report.index_created = await store.ensure_index()
    index, _ = await store.get_index()
    indexed = {
        str(name): str((entry or {}).get("state") or "")
        for name, entry in (index.get("changes") or {}).items()
    }
    active = ws.active()
    for change in active:
        try:
            item = await _migrate_change(store, change, indexed, dry_run=dry_run)
        except rs.RemoteError as exc:
            item = _remote_failure(exc)
        report.changes[change.name] = item
    vaults: dict[str, str] = {}
    for change in ws.archived():
        try:
            item = await _migrate_archived(
                store, change, indexed, vaults, dry_run=dry_run
            )
        except rs.RemoteError as exc:
            item = _remote_failure(exc)
        report.archived[change.name] = item
    local = rs.local_main_specs(ws)
    caps = set(local)
    for change in active:
        caps |= set(change.delta_files())
    pending = await _pending_caps(store, indexed)
    for cap in sorted(c for c in caps if rs.NAME_RE.match(c)):
        if cap in pending:
            report.mirrors[cap] = Item(SKIPPED, "pending_apply 合併中，鏡像領先 git")
            continue
        try:
            item = await _migrate_mirror(store, cap, local, dry_run=dry_run)
        except rs.RemoteError as exc:
            item = _remote_failure(exc)
        report.mirrors[cap] = item
    if dry_run:
        report.extras["decisions"] = Item(PLANNED, "推送 task-decisions")
        report.extras["snapshot"] = Item(PLANNED, "以服務端內容重算並推送任務快照")
        return report
    report.extras["decisions"] = await _push_decisions(store, ws)
    report.extras["snapshot"] = await _push_snapshot(store, ws)
    if report.ok:
        report.remote_mode = "enabled" if enable_remote(ws.root) else "already"
    return report


async def _push_decisions(store: rs.RemoteStore, ws: Workspace) -> Item:
    try:
        result = await remote_ops.push_decisions(store, ws)
    except rs.RemoteError as exc:
        return _remote_failure(exc)
    except rs.StoreError as exc:
        return Item(ERROR, exc.message)
    if result["status"] == "pushed":
        return Item(CREATED, f"{result['decisions']} 個 D 編號")
    return Item(SKIPPED, str(result.get("reason") or "與服務端一致"))


async def _push_snapshot(store: rs.RemoteStore, ws: Workspace) -> Item:
    try:
        result = await snapshot.push_remote(store, ws)
    except rs.RemoteError as exc:
        return _remote_failure(exc)
    except (rs.StoreError, snapshot.SnapshotTooLarge) as exc:
        return Item(ERROR, getattr(exc, "message", str(exc)))
    return Item(CREATED, f"{result.changes} 個 change，{result.size_bytes} 位元組")


# ── CLI ────────────────────────────────────────────────────────────


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "migrate", help="把既有 openspec/ 推成服務端版本化內容（可重跑、不覆寫）"
    )
    p.add_argument(
        "--vault", help="Lore Vault vault key（預設由專案目錄 binding 推算）"
    )
    p.add_argument("--dry-run", action="store_true", help="只讀服務端，列出會做的事")
    p.add_argument("--json", action="store_true")
    p.add_argument("--client-env", help="客戶端設定檔（預設 ~/.lore-vault/client.env）")


def run(
    args: argparse.Namespace,
    env: _Env,
    client_factory: Callable[[], VaultClient] | None,
) -> int:
    from .cli import EXIT_FAIL, EXIT_OK, _client_factory, _workspace

    ws = _workspace(args, env)
    if ws is None:
        return EXIT_FAIL
    client = (client_factory or _client_factory(args, env))()
    if not client.settings.push_configured:
        env.print(f"migrate 失敗：{client.describe()}")
        return EXIT_FAIL
    try:
        vault = snapshot.resolve_vault(client, ws, args.vault)
        store = rs.RemoteStore(rs.vault_client_post(client), vault)
        report = asyncio.run(migrate(ws, store, dry_run=args.dry_run))
    except ServiceError as exc:
        env.print(f"migrate 中止：{exc.detail}")
        return EXIT_FAIL
    except rs.RemoteUnreachable as exc:
        env.print(f"migrate 中止：服務不可達（{exc.detail}）")
        return EXIT_FAIL
    except rs.RemoteError as exc:
        env.print(f"migrate 中止：服務拒絕（{exc.code or exc.status}）：{exc.message}")
        return EXIT_FAIL
    except rs.StoreError as exc:
        env.print(f"migrate 中止：{exc.message}")
        return EXIT_FAIL
    if args.json:
        env.out.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    else:
        env.print(*report.lines())
    return EXIT_OK if report.ok else EXIT_FAIL
