"""任務層的 MCP 工具定義（TASK_LAYER_MCP §3）；由 `lore_vault.mcp.task_plugin` 掛載。

核心不 import 本模組；只有組合層 `mcp/task_plugin.py`（isolation 的唯一具名例外）
在 `mcp.tasks_enabled` 為真時動態載入，呼叫 `build_tools(shell)` 取得工具清單，
再以與核心工具相同的慣例（`structured_output=False`）註冊到 stdio 與 HTTP 兩邊的
server。

新增工具只需在 `build_tools` 回傳的清單加一筆 `TaskTool`：
- `name` 不可與核心工具（`lore_vault.mcp.server.TOOL_NAMES`）重名，plugin 會拒絕
- `fn` 是 async 函式，參數以 `Annotated[..., Field(description=...)]` 描述，最後一個
  參數為 `ctx: Context | None = None`，本體包在 `with shell.request_scope(ctx):`
  （HTTP 模式依 session 取目前 space 與轉發認證 header），回傳緊湊 JSON 字串
- 對服務的請求一律走 `shell` 既有的 async `ServiceClient`
  （經 `shell._send` 注入 space），不要另起同步 client

`tasks(action=)`：單一工具（比照 `space`），服務端內容的格式見 `remote_store`。
任務層固定在 dev space（與 MCP 目前 space 無關）。stdio 與 HTTP 的差異（§3.2）：

- vault：HTTP 必須帶（先 `vault_resolve(remote_url=...)`）；stdio 省略時用殼工作目錄
  的 binding
- 本機工作副本（`openspec/`）只在 stdio、且目標 vault 等於殼工作目錄 binding 時讀寫；
  HTTP 不碰任何本機檔案，回應不含本機路徑
- 主 spec 鏡像只由 stdio 的 init／validate 從本機 `specs/` 推送
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

import anyio
import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from lore_vault.binding import resolve_binding
from lore_vault.mcp.client import ServiceError, ServiceUnreachable
from lore_vault.mcp.server import (
    AUTHOR_FIELD_DESCRIPTION,
    _dump,
    _from_service_error,
    _tool_error,
)

from . import remote_store as rs
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
from .decisions import DECISION_ID
from .vault_client import ServiceError as HookServiceError
from .workspace import (
    DEFAULT_DIR,
    SPACE_DEV,
    STATUS_AUTH,
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    Workspace,
    default_meta,
    derive_status,
    load_workspace,
    meta_errors,
    record_base,
    resolve_root,
    trial_merge,
    validate_change,
)

if TYPE_CHECKING:
    from lore_vault.mcp.server import Shell


class TaskTool(NamedTuple):
    name: str
    fn: Callable[..., Any]
    description: str


ACTIONS = ("init", "propose", "edit", "pull", "list", "validate", "archive")
ARCHIVE_OP = "tasks_archive"
ARCHIVE_NEXT_STEP = (
    "這一步只規劃、沒有寫入。把 plan 給使用者看、取得明確同意後，才以完全相同的參數"
    "加上 confirm_token 再呼叫一次 archive；不要自動連打兩步。token 5 分鐘內有效"
)
PENDING_APPLY_NEXT_STEP = (
    "note 已寫入、服務端主 spec 鏡像已更新（已封存，待落地）；本機 specs/ 要等之後在"
    "有本機 repo 的機器以 stdio 殼同步（sync_specs）才會更新"
)
STATUS_PENDING_APPLY = "已封存（待落地）"
AUTH_REASON = "需艾斯維爾在 UI 任務頁核准後才能經 MCP archive"
HTTP_INIT_NOTE = (
    "此裝置之後如需本機檔案，另在該機器以 stdio 殼執行 tasks(action='init')"
)
GITIGNORE_COMMENT = (
    "# 任務層：change 工作內容以 Lore Vault 服務端為準（TASK_LAYER_MCP §5.4）"
)

# edit 可改的 metadata 欄位（其餘如 base／notes／vault 由 validate／archive 維護）
EDITABLE_META = (
    "goal",
    "source",
    "blocked_by",
    "depends_on",
    "requires_authorization",
    "skip_specs",
)

HINTS = {
    "vault_required": (
        "HTTP 端點沒有工作目錄：先用 vault_resolve(remote_url=<git remote get-url "
        "origin 的輸出>) 取得 key，再把 key 當 vault 傳入"
    ),
    "invalid_name": "change 名稱用 kebab-case，例如 add-login-page",
    "change_exists": "換一個名稱，或用 pull 取回既有的 change",
    "change_not_found": "先用 tasks(action='list') 確認名稱與 vault",
    "version_conflict": (
        "別人（或另一台機器）已改過這個 change：看錯誤附的 current（目前內容與版本），"
        "把你的修改合併上去後，以 current.version 當 expected_version 重送"
    ),
    "change_not_editable": "已封存（pending_apply）的 change 不能再編輯",
    "archive_in_progress": (
        "這個 change 的 archive 已開始（已寫入部分 note），內容不可再改；"
        "請重跑 archive 完成封存"
    ),
    "authorization_downgrade_forbidden": (
        "requires_authorization 不能經 MCP 從 true 改成 false；需要時請使用者本人處理"
    ),
    "no_changes": (
        "edit 至少要帶一個欄位（proposal_md／design_md／tasks_md／deltas 或 metadata）"
    ),
    "local_modified": (
        "本機工作副本有尚未推送的修改：先用 edit 把它推上服務端，"
        "或確認要捨棄本機修改後帶 overwrite=true 重新 pull"
    ),
    "mirror_missing": (
        "服務端沒有這個 capability 的主 spec 鏡像：在有本機 repo 的機器以 stdio 殼執行 "
        "tasks(action='validate') 推送後再試"
    ),
    "tasks_remote_sync_disabled": (
        "服務關閉了任務層遠端同步（tasks.remote_sync）：寫入類動作改用本機 CLI "
        "`python -m lore_vault.tasks`；讀取類動作（list／pull）不受影響"
    ),
    "too_large": (
        "change 內容（含所有 spec delta）上限 1MB；把過長的段落精簡或拆成多個 change"
    ),
    "index_conflict": "索引同時被多方更新，稍後重試",
    "authorization_required": (
        "這個 change 需要使用者本人核准：請使用者在 UI 的任務頁核准此 change 後再試；"
        "工具沒有、也不接受任何代填授權的參數"
    ),
    "authorization_stale": (
        "核准之後 change 又被修改過：把目前內容給使用者看，請他在 UI 重新核准後再試"
    ),
    "archive_rejected": "依 details 修正（edit／validate）後重新規劃 archive",
    "change_not_active": "已封存待落地的 change 不需要再 archive",
    "invalid_confirm_token": (
        "confirm_token 必須搭配規劃時完全相同的 vault／name／reason／allow_incomplete "
        "原樣送回；不確定就不帶 token 重新規劃，給使用者確認後再送"
    ),
    "confirm_token_expired": "token 已過期（5 分鐘）：重新規劃並再次取得使用者同意",
    "plan_changed": (
        "規劃後資料已變動、這次未執行：把錯誤附的新 plan 給使用者看，同意後才以"
        "附帶的新 confirm_token 重送"
    ),
    "mirror_changed": "重新規劃 archive（不帶 token）、給使用者確認後再送",
    "archive_write_failed": (
        "服務恢復後重新規劃 archive；已寫入的 note 會被沿用、不會重複"
    ),
    "invalid_remote_content": (
        "服務端內容格式不符；請使用者檢查（可能是舊版或手動寫入）"
    ),
}


def _post_for(shell: Shell) -> rs.Post:
    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await shell._send(path, body, space=SPACE_DEV)
        except ServiceUnreachable as exc:
            raise rs.RemoteUnreachable(exc.detail) from None
        except ServiceError as exc:
            raise rs.RemoteError(exc.status, exc.message, exc.body) from None

    return post


def _store_error(exc: rs.StoreError) -> ToolError:
    extra = dict(exc.extra)
    hint = HINTS.get(str(extra.pop("hint_code", "")) or exc.code)
    if isinstance(exc, rs.VersionConflict):
        extra["expected"] = exc.expected
        extra["current"] = (
            None
            if exc.current is None
            else {
                "version": exc.current.version,
                "state": exc.current.state,
                "change": _content_view(exc.current),
            }
        )
    return _tool_error(exc.code, exc.message, hint=hint, **extra)


def _remote_error(exc: rs.RemoteError) -> ToolError:
    tool_error = _from_service_error(
        ServiceError(exc.status or 0, exc.message, exc.body)
    )
    hint = HINTS.get(exc.code or "")
    if hint:
        payload = json.loads(str(tool_error))
        payload["hint"] = hint
        return ToolError(json.dumps(payload, ensure_ascii=False))
    return tool_error


def _content_view(change: rs.RemoteChange) -> dict[str, Any]:
    doc = change.to_doc()
    view = {
        "meta": doc.get("meta") or {},
        "proposal_md": doc.get("proposal_md") or "",
        "design_md": doc.get("design_md"),
        "tasks_md": doc.get("tasks_md") or "",
        "deltas": dict(doc.get("deltas") or {}),
    }
    if doc.get("apply"):
        view["apply"] = doc["apply"]
    return view


def _status(change: rs.RemoteChange, ws: rs.RemoteWorkspace) -> tuple[str, list[str]]:
    if change.state == rs.STATE_PENDING_APPLY:
        return STATUS_PENDING_APPLY, []
    status, reasons = derive_status(change, ws)
    if status == STATUS_AUTH:
        reasons = [AUTH_REASON]
    return status, reasons


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise rs.StoreError("invalid_request", f"{field} 必須是字串清單")
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def _decision_ids(values: list[str]) -> list[str]:
    bad = [d for d in values if not DECISION_ID.match(d)]
    if bad:
        raise rs.StoreError(
            "invalid_request", f"blocked_by 必須是 D 編號（例如 D6）：{', '.join(bad)}"
        )
    return values


@dataclass
class Target:
    vault: str
    store: rs.RemoteStore
    # stdio 且目標 vault 等於殼工作目錄 binding 時的本機任務目錄（可能尚未 init）
    local_root: Path | None
    local_reason: str | None = None

    def workspace(self) -> Workspace | None:
        root = self.local_root
        if root is None or not root.is_dir():
            return None
        return load_workspace(root, None, os.environ)


class TaskOps:
    """`tasks(action=)` 的邏輯（與 MCP 註冊分開，方便測試）。"""

    def __init__(self, shell: Shell) -> None:
        self.shell = shell
        self.post = _post_for(shell)
        self._confirm: Any = None

    # ── 共用 ──

    def _today(self) -> str:
        return self.shell._now().date().isoformat()

    def _cwd(self) -> Path:
        return Path(self.shell._cwd())

    async def _target(
        self, vault: str | None, *, create: bool = False, display: str | None = None
    ) -> Target:
        shell = self.shell
        if shell.http:
            if not vault:
                raise _tool_error(
                    "vault_required",
                    "HTTP 端點的 tasks 必須帶 vault",
                    hint=HINTS["vault_required"],
                )
            resolved = await shell.vault_resolve(
                key=vault, create=create, display=display, space=SPACE_DEV
            )
            key = str(resolved["key"])
            return Target(key, rs.RemoteStore(self.post, key), None)
        resolved = await shell.vault_resolve(
            key=vault or None, create=create, display=display, space=SPACE_DEV
        )
        key = str(resolved["key"])
        local_root = resolve_root(None, os.environ, self._cwd()) or (
            self._cwd() / DEFAULT_DIR
        )
        reason = None
        if vault:
            # 明確指定的 vault 必須就是殼工作目錄的 binding，才碰本機檔案
            if not await self._is_cwd_vault(key):
                local_root, reason = (
                    None,
                    "vault 與殼工作目錄的 binding 不同，不讀寫本機檔案",
                )
        return Target(key, rs.RemoteStore(self.post, key), local_root, reason)

    async def _is_cwd_vault(self, key: str) -> bool:
        try:
            binding = resolve_binding(str(self._cwd()))
        except (OSError, ValueError):
            return False
        if binding.key == key:
            return True
        try:
            data = await self.post("/v1/vault_resolve", {"key": binding.key})
        except (rs.RemoteError, rs.RemoteUnreachable):
            return False
        return str(data.get("key")) == key

    def _local_skipped(self, target: Target, reason: str | None = None) -> dict:
        return {
            "written": False,
            "reason": reason
            or target.local_reason
            or "本機沒有任務目錄（先在 stdio 執行 init）",
        }

    async def _remote_workspace(
        self,
        target: Target,
        changes: list[rs.RemoteChange],
        archived: set[str],
        mirror_for: list[rs.RemoteChange] = (),  # type: ignore[assignment]
    ) -> rs.RemoteWorkspace:
        """`mirror_for`：要讀主 spec 鏡像的 change（validate／archive）；
        list 不需要。"""
        decisions = None
        local = None if self.shell.http else target.workspace()
        if local is not None:
            decisions = local.decisions()
            archived = archived | {c.name for c in local.archived()}
        caps = sorted({cap for c in mirror_for for cap in c.deltas})
        mirrors = {}
        for cap in caps:
            mirror = await target.store.get_mirror(cap)
            if mirror is not None:
                mirrors[cap] = mirror
        return rs.RemoteWorkspace(
            root=PurePosixPath("remote"),  # type: ignore[arg-type]
            changes=changes,
            mirrors=mirrors,
            archived_names=archived,
            decisions_map=decisions,
        )

    async def _push_mirrors(
        self, target: Target, ws: Workspace, changes: list[rs.RemoteChange]
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
            mirror = await target.store.get_mirror(cap)
            if mirror is not None and mirror.same_content(exists, text):
                report["unchanged"].append(cap)
                continue
            try:
                await target.store.put_mirror(
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

    # ── actions ──

    async def init(self, vault: str | None, display: str | None) -> dict[str, Any]:
        target = await self._target(vault, create=True, display=display)
        created = await target.store.ensure_index()
        result: dict[str, Any] = {
            "vault": target.vault,
            "space": SPACE_DEV,
            "created": created,
        }
        if self.shell.http:
            result["note"] = HTTP_INIT_NOTE
            return result
        if target.local_root is None:
            result["local"] = self._local_skipped(target)
            return result
        from .cli import init_root

        root = target.local_root
        files = init_root(root)
        ws = load_workspace(root, None, os.environ)
        changes, _ = await target.store.list_changes()
        result["local"] = {
            "root": str(root),
            "created": files,
            "gitignore": _ensure_gitignore(root),
        }
        result["mirrors"] = await self._push_mirrors(target, ws, changes)
        return result

    async def propose(
        self,
        vault: str | None,
        name: str | None,
        *,
        goal: str | None,
        source: str | None,
        blocked_by: list[str] | None,
        depends_on: list[str] | None,
        requires_authorization: bool | None,
        skip_specs: bool | None,
    ) -> dict[str, Any]:
        from .cli import _PROPOSAL_TEMPLATE, _TASKS_TEMPLATE

        name = rs.check_name(name)
        blocked = _decision_ids(_str_list(blocked_by or [], "blocked_by"))
        depends = _str_list(depends_on or [], "depends_on")
        target = await self._target(vault)
        ws = target.workspace()
        index, _ = await target.store.get_index()
        local_archived = {c.name for c in ws.archived()} if ws else set()
        if name in (index.get("changes") or {}) or name in local_archived:
            raise rs.StoreError(
                "change_exists", f"change {name} 已存在（vault {target.vault}）"
            )
        meta = default_meta(
            self._today(),
            source=source or None,
            blocked_by=blocked,
            depends_on=depends,
            requires_authorization=bool(requires_authorization),
            skip_specs=bool(skip_specs),
        )
        if goal:
            meta = {
                "schema": meta.pop("schema"),
                "created": meta.pop("created"),
                "goal": goal,
                **meta,
            }
        source_line = f"> 來源：{source}\n\n" if source else ""
        doc = rs.new_doc(
            name,
            meta,
            _PROPOSAL_TEMPLATE.format(source_line=source_line),
            _TASKS_TEMPLATE,
        )
        change = await target.store.create_change(doc)
        archived = {
            n
            for n, entry in (index.get("changes") or {}).items()
            if (entry or {}).get("state") != rs.STATE_ACTIVE
        }
        rws = await self._remote_workspace(target, [change], archived)
        status, reasons = _status(change, rws)
        result: dict[str, Any] = {
            "name": name,
            "vault": target.vault,
            "version": change.version,
            "state": change.state,
            "status": status,
            "reasons": reasons,
            "next_step": (
                "無規格的純任務：用 edit 改寫 proposal_md／tasks_md，"
                "勾完 tasks 後再 archive"
                if skip_specs
                else "用 edit 帶 deltas={capability: spec delta 全文} 新增規格變更，"
                "再以 validate(name=..., record_base=true) 記錄 base"
            ),
        }
        if not self.shell.http:
            result["local"] = self._write_new_local(target, ws, change)
        return result

    def _write_new_local(
        self, target: Target, ws: Workspace | None, change: rs.RemoteChange
    ) -> dict[str, Any]:
        if ws is None:
            return self._local_skipped(target)
        if (ws.changes_dir / change.name).exists():
            return self._local_skipped(
                target, "本機已有同名目錄（尚未同步的既有 change？），未覆寫"
            )
        path = rs.write_local(ws, change)
        return {"written": True, "path": str(path)}

    async def edit(
        self,
        vault: str | None,
        name: str | None,
        expected_version: int | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        name = rs.check_name(name)
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise rs.StoreError(
                "invalid_request",
                "edit 必須帶 expected_version（pull／propose 回傳的 version）",
            )
        given = {k: v for k, v in fields.items() if v is not None}
        if not given:
            raise rs.StoreError("no_changes", "edit 沒有任何要更新的欄位")
        target = await self._target(vault)
        change = await target.store.require_change(name)
        if change.state != rs.STATE_ACTIVE:
            raise rs.StoreError(
                "change_not_editable", f"change {name} 狀態為 {change.state}，不可編輯"
            )
        if change.version != expected_version:
            raise rs.VersionConflict(name, expected_version, change)
        meta = change.meta
        if meta.get("notes") or meta.get("note_digests"):
            raise rs.StoreError(
                "archive_in_progress", f"change {name} 的 archive 已開始，內容不可再改"
            )
        if given.get("requires_authorization") is False and meta.get(
            "requires_authorization"
        ):
            raise rs.StoreError(
                "authorization_downgrade_forbidden",
                f"change {name} 標記 requires_authorization: true，MCP 不可取消",
            )
        ws = None if self.shell.http else target.workspace()
        before = rs.local_state(ws, change) if ws is not None else None
        _apply_edit(change, given)
        # 服務端樂觀鎖以呼叫端讀到的版本比對（上面的比對只是提早回報）
        change.version = expected_version
        await target.store.save_change(change)
        result: dict[str, Any] = {
            "name": name,
            "vault": target.vault,
            "version": change.version,
            "updated": sorted(given),
        }
        if not self.shell.http:
            if ws is None or before is None:
                result["local"] = self._local_skipped(target)
            elif before.state == "absent":
                result["local"] = {
                    "written": False,
                    "reason": "本機沒有這個 change 的工作副本",
                }
            elif before.safe_to_overwrite:
                path = rs.write_local(ws, change)
                result["local"] = {"written": True, "path": str(path)}
            else:
                result["local"] = {
                    "written": False,
                    "state": before.state,
                    "reason": (
                        "本機工作副本有未推送的修改，未覆寫"
                        "（之後用 pull overwrite=true 對齊）"
                    ),
                }
        return result

    async def pull(
        self, vault: str | None, name: str | None, overwrite: bool
    ) -> dict[str, Any]:
        name = rs.check_name(name)
        target = await self._target(vault)
        change = await target.store.require_change(name)
        result: dict[str, Any] = {
            "name": name,
            "vault": target.vault,
            "version": change.version,
            "state": change.state,
            "change": _content_view(change),
        }
        if self.shell.http:
            return result
        ws = target.workspace()
        if ws is None:
            result["local"] = self._local_skipped(target)
        elif change.state != rs.STATE_ACTIVE:
            result["local"] = {
                "written": False,
                "reason": (
                    "已封存待落地：本機 specs 由 sync_specs 落地，不寫回 changes/"
                ),
            }
        else:
            state = rs.local_state(ws, change)
            if not state.safe_to_overwrite and not overwrite:
                raise rs.StoreError(
                    "local_modified",
                    f"本機 changes/{name} 有未推送的修改（{state.state}），未覆寫",
                    local_state=state.state,
                    remote_version=change.version,
                )
            path = rs.write_local(ws, change)
            result["local"] = {
                "written": True,
                "path": str(path),
                "previous": state.state,
            }
        return result

    async def list_(
        self, vault: str | None, status_filter: str | None
    ) -> dict[str, Any]:
        if vault == "*":
            rows: list[dict[str, Any]] = []
            for key in sorted(await rs.all_indexes(self.post)):
                target = Target(key, rs.RemoteStore(self.post, key), None)
                rows += [{"vault": key, **row} for row in await self._rows(target)]
            return {"vault": "*", "changes": _filter(rows, status_filter)}
        target = await self._target(vault)
        rows = await self._rows(target)
        return {"vault": target.vault, "changes": _filter(rows, status_filter)}

    async def _rows(self, target: Target) -> list[dict[str, Any]]:
        changes, archived = await target.store.list_changes()
        rws = await self._remote_workspace(target, changes, archived)
        rows = []
        for change in changes:
            status, reasons = _status(change, rws)
            done, total = change.tasks_progress()
            rows.append(
                {
                    "name": change.name,
                    "state": change.state,
                    "status": status,
                    "reasons": reasons,
                    "blocked_by": list(change.meta.get("blocked_by") or []),
                    "depends_on": list(change.meta.get("depends_on") or []),
                    "requires_authorization": bool(
                        change.meta.get("requires_authorization")
                    ),
                    "tasks": f"{done}/{total}",
                    "version": change.version,
                }
            )
        return rows

    async def validate(
        self,
        vault: str | None,
        name: str | None,
        *,
        record: bool,
        rebase: bool,
    ) -> dict[str, Any]:
        if (record or rebase) and not name:
            raise rs.StoreError(
                "invalid_request", "record_base／rebase 只能搭配單一 name 使用"
            )
        if record and rebase:
            raise rs.StoreError("invalid_request", "record_base 與 rebase 擇一")
        target = await self._target(vault)
        changes, archived = await target.store.list_changes()
        result: dict[str, Any] = {"vault": target.vault}
        local = None if self.shell.http else target.workspace()
        if local is not None:
            result["mirrors"] = await self._push_mirrors(target, local, changes)
        active = [c for c in changes if c.state == rs.STATE_ACTIVE]
        if name:
            rs.check_name(name)
            targets = [c for c in active if c.name == name]
            if not targets:
                raise rs.StoreError(
                    "change_not_found",
                    f"沒有 active change {name}（vault {target.vault}）",
                )
        else:
            targets = active
        rws = await self._remote_workspace(target, changes, archived, targets)
        rows = []
        for change in targets:
            row: dict[str, Any] = {"name": change.name}
            missing = (
                [] if change.meta.get("skip_specs") else rws.missing_mirrors(change)
            )
            if (record or rebase) and not missing and not change.meta_error:
                changed = record_base(change, rws, overwrite=rebase)
                if changed:
                    await target.store.save_change(change)
                    row["base_recorded"] = changed
            if missing:
                errors = meta_errors(change) + [
                    f"{cap}：服務端沒有主 spec 鏡像（在有本機 repo 的機器以 stdio "
                    "執行 tasks validate 推送）"
                    for cap in missing
                ]
            else:
                errors = validate_change(change, rws, rws.active())
            row.update({"version": change.version, "ok": not errors, "errors": errors})
            if local is not None:
                row["local_sync"] = rs.local_state(local, change).state
            rows.append(row)
        result["ok"] = all(r["ok"] for r in rows)
        result["results"] = rows
        return result

    # ── archive（段一＋授權閘門）──

    def _signer(self) -> Any:
        if self._confirm is None:
            # 沿用服務端破壞性操作的 token 簽章（api.manage）；延遲載入，
            # 只有用到 archive 時 stdio 殼才載入 API 層
            from lore_vault.api.manage import ConfirmSigner

            self._confirm = ConfirmSigner(clock=lambda: _token_clock())
        return self._confirm

    async def _gate(
        self, store: rs.RemoteStore, change: rs.RemoteChange
    ) -> rs.AuthorizationRecord | None:
        """§3.3 授權閘門：`requires_authorization` 的 change 必須有 UI 核准紀錄，
        且核准的是目前版本（archive 已開始時以 meta 記下的核准版本比對）。
        只讀授權紀錄，不做其他服務呼叫。"""
        if not change.meta.get("requires_authorization"):
            return None
        try:
            record = await store.get_authorization(change.name)
        except rs.StoreError as exc:
            raise rs.StoreError("authorization_required", exc.message) from None
        if record is None:
            raise rs.StoreError(
                "authorization_required",
                f"{change.name} 標記 requires_authorization: true，"
                "尚未有使用者在 UI 核准的紀錄",
            )
        started = change.meta.get("authorization")
        if (
            isinstance(started, dict)
            and started.get("change_version") == record.change_version
        ):
            return record
        if record.change_version != change.version:
            raise rs.StoreError(
                "authorization_stale",
                f"{change.name} 的核准針對 v{record.change_version}，"
                f"目前已是 v{change.version}（核准後內容又被修改）",
                authorized_version=record.change_version,
                current_version=change.version,
            )
        return record

    async def archive(
        self,
        vault: str | None,
        name: str | None,
        *,
        reason: str | None,
        allow_incomplete: bool,
        confirm_token: str | None,
        author: str | None,
    ) -> dict[str, Any]:
        from lore_vault.api.errors import ConfirmTokenExpired, ConfirmTokenInvalid
        from lore_vault.api.manage import plan_digest

        name = rs.check_name(name)
        if self.shell.http and not vault:
            raise _tool_error(
                "vault_required",
                "HTTP 端點的 tasks 必須帶 vault",
                hint=HINTS["vault_required"],
            )
        token_args = {
            "vault": vault or "",
            "name": name,
            "reason": reason or "",
            "allow_incomplete": bool(allow_incomplete),
        }
        signer = self._signer()
        expected_digest = None
        if confirm_token is not None:
            try:
                expected_digest = signer.verify(confirm_token, ARCHIVE_OP, token_args)
            except ConfirmTokenInvalid as exc:
                raise _tool_error(
                    "invalid_confirm_token",
                    str(exc),
                    hint=HINTS["invalid_confirm_token"],
                ) from None
            except ConfirmTokenExpired as exc:
                raise _tool_error(
                    "confirm_token_expired",
                    str(exc),
                    hint=HINTS["confirm_token_expired"],
                ) from None
        # 1. 授權閘門：只讀 change 與授權紀錄（vault 用呼叫端給的 key 或工作目錄
        #    binding，服務端會解析別名），在 vault_resolve／list／write 之前
        gate_store = rs.RemoteStore(self.post, vault or self._binding_key())
        change = await gate_store.require_change(name)
        if change.state != rs.STATE_ACTIVE:
            raise rs.StoreError(
                "change_not_active",
                f"change {name} 狀態為 {change.state}，不是 active（已封存待落地）",
            )
        record = await self._gate(gate_store, change)
        # 2. 全驗與規劃
        target = await self._target(vault)
        change.vault = target.vault
        plan, merged, mirrors = await self._archive_plan(
            target, change, record, allow_incomplete=allow_incomplete, reason=reason
        )
        digest = plan_digest(plan)
        if expected_digest is None:
            issued, expires = signer.issue(ARCHIVE_OP, token_args, digest)
            return {
                "executed": False,
                "plan": plan,
                "confirm_token": issued,
                "expires_at": _utc(expires),
                "next_step": ARCHIVE_NEXT_STEP,
            }
        if digest != expected_digest:
            issued, expires = signer.issue(ARCHIVE_OP, token_args, digest)
            raise _tool_error(
                "plan_changed",
                "規劃後 change 或主 spec 鏡像已變動，這次未執行",
                hint=HINTS["plan_changed"],
                plan=plan,
                confirm_token=issued,
                expires_at=_utc(expires),
            )
        return await self._archive_execute(
            target, change, record, merged, mirrors, reason=reason, author=author
        )

    def _binding_key(self) -> str:
        try:
            return resolve_binding(str(self._cwd())).key
        except (OSError, ValueError) as exc:
            raise _tool_error("invalid_cwd", str(exc)) from None

    async def _archive_plan(
        self,
        target: Target,
        change: rs.RemoteChange,
        record: rs.AuthorizationRecord | None,
        *,
        allow_incomplete: bool,
        reason: str | None,
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, rs.Mirror]]:
        """archive 段一的全驗（同本機 archive 第 1b～2 步，主 spec 讀鏡像）。
        回傳 (規劃, 尚待推進的 {cap: 併入後全文}, 讀到的鏡像)。只讀不寫。"""
        meta = change.meta
        try:
            check_incomplete(change, allow_incomplete)
        except ArchiveError as exc:
            raise _rejected(exc) from None
        changes, archived = await target.store.list_changes()
        changes = [c for c in changes if c.name != change.name] + [change]
        rws = await self._remote_workspace(target, changes, archived, [change])
        applied = list(meta.get(rs.MIRROR_APPLIED_KEY) or [])
        _reconcile_mirror_applying(change, rws, applied)
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
            raise _rejected(exc) from None
        caps = sorted(set(merged) | set(applied))
        plan = {
            "name": change.name,
            "vault": target.vault,
            "version": change.version,
            "requires_authorization": bool(meta.get("requires_authorization")),
            "authorized_by": record.authorized_by if record else None,
            "authorized_version": record.change_version if record else None,
            "incomplete": meta.get("incomplete_at_archive"),
            "reason": reason or None,
            "requirements": [k for k, *_ in _requirement_items(change)],
            "removed": _removed_keys(change),
            "already_written": sorted(meta.get("notes") or {}),
            "capabilities": caps,
            "mirror_versions": {cap: rws.mirrors[cap].version for cap in caps},
            "merged_sha256": {
                cap: _sha256(merged.get(cap) or rws.mirrors[cap].text or "")
                for cap in caps
            },
        }
        return plan, merged, rws.mirrors

    async def _archive_execute(
        self,
        target: Target,
        change: rs.RemoteChange,
        record: rs.AuthorizationRecord | None,
        merged: dict[str, str],
        mirrors: dict[str, rs.Mirror],
        *,
        reason: str | None,
        author: str | None,
    ) -> dict[str, Any]:
        store = target.store
        meta = change.meta
        name = change.name
        if meta.get("vault") not in (None, target.vault):
            raise rs.StoreError(
                "archive_rejected",
                f"vault 與先前記錄不同：{meta.get('vault')} → {target.vault}",
            )
        meta["vault"] = target.vault
        if record is not None and not isinstance(meta.get("authorization"), dict):
            meta["authorization"] = record.to_meta()
        if reason:
            meta["archive_reason"] = reason
        await store.save_change(change)
        result = ArchiveResult(
            name=name, destination="", note_id="", vault=target.vault
        )
        client = rs.ThreadBridgeClient(store)

        def write() -> None:
            change.saver = lambda: anyio.from_thread.run(store.save_change, change)
            try:
                write_notes(
                    change,
                    client,  # type: ignore[arg-type]
                    target.vault,
                    SPACE_DEV,
                    authorized_by=record.authorized_by if record else None,
                    author=author or DEFAULT_AUTHOR,
                    result=result,
                )
            finally:
                change.saver = None

        try:
            await anyio.to_thread.run_sync(write)
        except ArchiveError as exc:
            raise _rejected(exc) from None
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
            meta[rs.MIRROR_APPLYING_KEY] = {cap: _sha256(text)}
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
        stamp = self.shell._now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            "executed": True,
            "name": name,
            "vault": target.vault,
            "version": change.version,
            "state": rs.STATE_PENDING_APPLY,
            "status": STATUS_PENDING_APPLY,
            "note_id": meta.get("note_id"),
            "written": result.written,
            "skipped": result.skipped,
            "capabilities": sorted(merged_specs),
            "next_step": PENDING_APPLY_NEXT_STEP,
        }


def _filter(rows: list[dict[str, Any]], status_filter: str | None) -> list[dict]:
    if not status_filter:
        return rows
    return [r for r in rows if status_filter in (r["status"], r["state"])]


def _token_clock() -> float:
    return time.time()


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rejected(exc: ArchiveError) -> rs.StoreError:
    return rs.StoreError("archive_rejected", str(exc), details=list(exc.details))


def _reconcile_mirror_applying(
    change: rs.RemoteChange, ws: rs.RemoteWorkspace, applied: list[str]
) -> None:
    """續跑：`mirror_applying` 記的鏡像若已是記錄的內容（推進成功、還沒記進
    `mirror_applied_caps` 就中斷），補記為已套用（只改記憶體，執行時才落地）。"""
    applying = change.meta.get(rs.MIRROR_APPLYING_KEY)
    if not isinstance(applying, dict) or not applying:
        return
    for cap, digest in applying.items():
        mirror = ws.mirrors.get(str(cap))
        if mirror is None or not mirror.exists or _sha256(mirror.text or "") != digest:
            return
    for cap in applying:
        if cap not in applied:
            applied.append(str(cap))
    change.meta[rs.MIRROR_APPLIED_KEY] = list(applied)
    change.meta.pop(rs.MIRROR_APPLYING_KEY, None)


def _apply_edit(change: rs.RemoteChange, given: dict[str, Any]) -> None:
    doc = change.doc
    meta = change.meta
    for fld in ("proposal_md", "tasks_md"):
        if fld in given:
            if not isinstance(given[fld], str):
                raise rs.StoreError("invalid_request", f"{fld} 必須是字串")
            doc[fld] = given[fld]
    if "design_md" in given:
        value = given["design_md"]
        if not isinstance(value, str):
            raise rs.StoreError("invalid_request", "design_md 必須是字串")
        doc["design_md"] = value or None
    if "deltas" in given:
        deltas = given["deltas"]
        if not isinstance(deltas, dict):
            raise rs.StoreError("invalid_request", "deltas 必須是 {capability: 全文}")
        current = dict(doc.get("deltas") or {})
        for cap, text in deltas.items():
            rs.check_capability(cap)
            if text is None:
                current.pop(cap, None)
            elif isinstance(text, str):
                current[cap] = text
            else:
                raise rs.StoreError(
                    "invalid_request", f"deltas[{cap!r}] 必須是字串或 null"
                )
        doc["deltas"] = dict(sorted(current.items()))
    for key in ("goal", "source"):
        if key in given:
            value = given[key]
            if not isinstance(value, str):
                raise rs.StoreError("invalid_request", f"{key} 必須是字串")
            if value.strip():
                meta[key] = value.strip()
            else:
                meta.pop(key, None)
    if "blocked_by" in given:
        meta["blocked_by"] = _decision_ids(_str_list(given["blocked_by"], "blocked_by"))
    if "depends_on" in given:
        meta["depends_on"] = _str_list(given["depends_on"], "depends_on")
    for key in ("requires_authorization", "skip_specs"):
        if key in given:
            if not isinstance(given[key], bool):
                raise rs.StoreError("invalid_request", f"{key} 必須是 true／false")
            meta[key] = given[key]


def _ensure_gitignore(root: Path) -> dict[str, Any]:
    """stdio init：把 `<任務目錄>/changes/` 加進專案 `.gitignore`（§5.4，可否決）；
    已有涵蓋它的規則（整個任務目錄或 changes/）時不動。"""
    project = root.parent
    entry = f"{root.name}/changes/"
    if not ((project / ".git").exists() or (project / ".gitignore").exists()):
        return {"status": "skipped", "entry": entry, "reason": "不是 git repo"}
    path = project / ".gitignore"
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    covering = {
        f"{prefix}{name}{suffix}"
        for prefix in ("", "/")
        for name in (root.name, f"{root.name}/changes")
        for suffix in ("", "/")
    }
    if any(line.strip() in covering for line in text.splitlines()):
        return {"status": "covered", "entry": entry}
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = "" if not text or text.endswith(("\n", "\r")) else newline
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(f"{prefix}{GITIGNORE_COMMENT}{newline}{entry}{newline}")
    return {"status": "added", "entry": entry}


DESCRIPTION = (
    "任務層（OpenSpec 格式的 change／spec delta，Lore Vault 服務端為權威、跨裝置共用；"
    "固定在 dev space，與目前 space 無關）。用 action 選動作，其餘參數依動作帶：\n"
    "- init(vault?, display?)：建立此 vault 的任務層（vault 不存在時以 display 建立）。"
    "本地 stdio 殼另在工作目錄建 openspec/ 骨架、把 openspec/changes/ 加進 .gitignore，"
    "並把本機主 spec 推成服務端鏡像\n"
    "- propose(name, goal?, source?, blocked_by?, depends_on?, "
    "requires_authorization?, skip_specs?)：建立 change（version=1，含 proposal／"
    "tasks 範本）。name 用 kebab-case；"
    "blocked_by 是 DECISIONS 的 D 編號；depends_on 是其他 change 名稱\n"
    "- edit(name, expected_version, proposal_md?, design_md?, tasks_md?, deltas?, "
    "goal?, "
    "source?, blocked_by?, depends_on?, requires_authorization?, skip_specs?)：整段取代"
    "給了的欄位（deltas={capability: delta 全文}，值為 null 刪除該 capability；"
    "design_md 給空字串刪除）。expected_version 必須是你最後讀到的 version；"
    "別人先改過會"
    "回 version_conflict 並附目前內容，合併後以新 version 重送，不要盲目覆蓋。"
    "requires_authorization 只能由 false 改 true\n"
    "- pull(name, overwrite?)：取回 change 目前完整內容與 version。本地 stdio 殼另寫回"
    "本機 openspec/changes/<name>/；本機有未推送修改時回 local_modified，"
    "確認捨棄才帶 overwrite=true\n"
    "- list(vault?, status_filter?)：change 狀態表（可開工／被擋住／待授權／無法判定／"
    "已封存（待落地））；vault='*' 列出目前 space 所有 vault\n"
    "- validate(name?, record_base?, rebase?)：檢查格式、requirement 重疊、base 是否"
    "過時與 delta 併回主 spec 的試算（主 spec 讀服務端鏡像）；"
    "省略 name 檢查全部 active。新增 delta 後帶 record_base=true 記錄 base；"
    "依主 spec 現值改好 delta 後帶 rebase=true"
    "重記。本地 stdio 殼會先把本機 specs/ 推成鏡像\n"
    "vault：本地 stdio 殼可省略（用工作目錄的專案）；"
    "HTTP 端點必須帶 vault_resolve 回傳的"
    " key。\n"
    "- archive(name, reason?, allow_incomplete?, confirm_token?, author?)：封存。"
    "兩步式："
    "(1) 不帶 confirm_token → 全部驗證後只回 plan 與 confirm_token（不寫入）；"
    "(2) 把 plan 給使用者看、取得明確同意後，才以完全相同的參數加上 confirm_token "
    "再呼叫一次。不要自動連打兩步；規劃後資料變動會回 plan_changed（附新 plan 與新 "
    "token，仍需使用者再確認）。成功後寫入 requirement／總結 note、服務端主 spec 鏡像"
    "更新為併入後內容，change 變成「已封存（待落地）」（pending_apply），本機 specs/ "
    "之後由有本機 repo 的機器同步。requires_authorization 的 change 必須先由使用者"
    "本人在 UI 任務頁核准（核准後再改內容需重新核准）；沒有核准會回 "
    "authorization_required——不要自行代填或繞過\n"
    "本機檔案只在 stdio、且 vault 就是工作目錄專案時讀寫。HTTP 端點讀不到 "
    "DECISIONS.md，有 blocked_by 的 change 狀態會是「無法判定」。"
)


def build_tools(shell: Shell) -> list[TaskTool]:
    """任務層要掛上 MCP server 的工具：單一 `tasks(action=)`。"""
    ops = TaskOps(shell)

    async def tasks(
        action: Annotated[
            str,
            Field(description="動作：" + "／".join(ACTIONS)),
        ],
        vault: Annotated[
            str | None,
            Field(
                description="vault key（vault_resolve 的回傳）。stdio 可省略＝工作目錄"
                "的專案；HTTP 必填；list 可用 '*'"
            ),
        ] = None,
        name: Annotated[
            str | None, Field(description="change 名稱（kebab-case）")
        ] = None,
        display: Annotated[
            str | None, Field(description="init：vault 不存在時建立用的顯示名稱")
        ] = None,
        goal: Annotated[
            str | None,
            Field(description="propose／edit：一句話目標（edit 給空字串刪除）"),
        ] = None,
        source: Annotated[
            str | None,
            Field(description="propose／edit：對應的 TASKS 卡號，例如 T-32"),
        ] = None,
        blocked_by: Annotated[
            list[str] | None,
            Field(
                description="propose／edit：阻塞此 change 的 D 編號清單，例如 ['D6']"
            ),
        ] = None,
        depends_on: Annotated[
            list[str] | None,
            Field(description="propose／edit：必須先封存的其他 change 名稱清單"),
        ] = None,
        requires_authorization: Annotated[
            bool | None,
            Field(
                description="propose／edit：封存前須使用者本人在 UI 核准；"
                "edit 只能由 false 改 true"
            ),
        ] = None,
        skip_specs: Annotated[
            bool | None,
            Field(description="propose／edit：無規格的純任務（不帶 spec delta）"),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(
                description="edit 必填：你最後讀到的 version（propose／pull 的回傳）"
            ),
        ] = None,
        proposal_md: Annotated[
            str | None, Field(description="edit：proposal.md 全文（整段取代）")
        ] = None,
        design_md: Annotated[
            str | None,
            Field(description="edit：design.md 全文（整段取代；空字串刪除）"),
        ] = None,
        tasks_md: Annotated[
            str | None,
            Field(description="edit：tasks.md 全文（整段取代；`- [x]` 為已完成）"),
        ] = None,
        deltas: Annotated[
            dict[str, str | None] | None,
            Field(
                description="edit：{capability: spec delta 全文}（## ADDED／MODIFIED／"
                "REMOVED Requirements）；只更新給了的 capability，值為 null 刪除"
            ),
        ] = None,
        status_filter: Annotated[
            str | None,
            Field(description="list：只列此狀態（例如 可開工、active、pending_apply）"),
        ] = None,
        record_base: Annotated[
            bool,
            Field(description="validate（須帶 name）：補記尚未記錄的 base"),
        ] = False,
        rebase: Annotated[
            bool,
            Field(
                description="validate（須帶 name）："
                "delta 已依主 spec 現值改好後重記 base"
            ),
        ] = False,
        reason: Annotated[
            str | None,
            Field(description="archive：封存原因（記進 change metadata）"),
        ] = None,
        allow_incomplete: Annotated[
            bool,
            Field(
                description="archive：tasks_md 尚有未勾選項目時仍封存（未完成數會記下）"
            ),
        ] = False,
        confirm_token: Annotated[
            str | None,
            Field(
                description="archive 第二步才帶：第一步回傳的 confirm_token。"
                "只有在使用者"
                "看過規劃並明確同意後才可帶上；其餘參數必須與第一步完全相同"
            ),
        ] = None,
        author: Annotated[
            str | None, Field(description="archive：" + AUTHOR_FIELD_DESCRIPTION)
        ] = None,
        overwrite: Annotated[
            bool,
            Field(description="pull（stdio）：捨棄本機未推送的修改，以服務端內容覆寫"),
        ] = False,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            try:
                if action == "init":
                    result = await ops.init(vault, display)
                elif action == "propose":
                    result = await ops.propose(
                        vault,
                        name,
                        goal=goal,
                        source=source,
                        blocked_by=blocked_by,
                        depends_on=depends_on,
                        requires_authorization=requires_authorization,
                        skip_specs=skip_specs,
                    )
                elif action == "edit":
                    result = await ops.edit(
                        vault,
                        name,
                        expected_version,
                        {
                            "proposal_md": proposal_md,
                            "design_md": design_md,
                            "tasks_md": tasks_md,
                            "deltas": deltas,
                            "goal": goal,
                            "source": source,
                            "blocked_by": blocked_by,
                            "depends_on": depends_on,
                            "requires_authorization": requires_authorization,
                            "skip_specs": skip_specs,
                        },
                    )
                elif action == "pull":
                    result = await ops.pull(vault, name, overwrite)
                elif action == "list":
                    result = await ops.list_(vault, status_filter)
                elif action == "validate":
                    result = await ops.validate(
                        vault, name, record=record_base, rebase=rebase
                    )
                elif action == "archive":
                    result = await ops.archive(
                        vault,
                        name,
                        reason=reason,
                        allow_incomplete=allow_incomplete,
                        confirm_token=confirm_token,
                        author=author,
                    )
                else:
                    raise _tool_error(
                        "invalid_request",
                        f"action 必須是 {list(ACTIONS)} 之一，得到 {action!r}",
                    )
            except rs.StoreError as exc:
                raise _store_error(exc) from None
            except rs.RemoteError as exc:
                raise _remote_error(exc) from None
            except rs.RemoteUnreachable as exc:
                raise _tool_error(
                    "service_unreachable",
                    f"服務不可達（{exc.detail}），任務層動作未執行",
                    hint="服務恢復後重試；寫入類動作重試前先 pull 確認是否已生效",
                ) from None
            return _dump(result)

    return [TaskTool("tasks", tasks, DESCRIPTION)]
