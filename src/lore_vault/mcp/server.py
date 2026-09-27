"""本地 stdio MCP 殼（A15）：十個工具轉發到服務 HTTP，服務不可達時讀本地快照降級。

工具刻意只有 `space`、`vault_resolve`、`recall`、`ask`、`get`、`list`、`write`、
`update`、`upload`、`status`；建 vault 併入 `vault_resolve(create=True)`，不另開工具。
不暴露 chat／model／settings／source。

`ask`（D11）：薄殼轉發 `POST /v1/ask`，逾時用 `mcp.ask_timeout`（要等模型）。
問答需要服務端模型，服務不可達時**不降級**（快照沒有模型），直接回工具錯誤。

`upload`（T-67）：殼讀本機檔案、以 multipart 轉送 `POST /v1/documents`。只能讀
`upload_roots`（殼的工作目錄＋設定 `mcp.upload_roots`）底下的一般檔案：路徑含 `..`
一律拒絕；以 realpath（解開 symlink／junction）比對，逃出白名單回 `path_not_allowed`。

「目前 space」（A18）由殼持有：每個殼行程一份、不持久化，新行程一律 `dev`。
`space` 工具查詢／切換（不打服務）；其他工具不帶 space 參數，由殼在每個服務請求
自動注入（服務端 space 必填、無預設）。降級讀快照時同樣以目前 space 過濾。

- 成功：回服務的 JSON 原樣（`vault_resolve` 另附 `binding`、`status` 另附 `shell`）
- 服務明確拒絕（4xx、3xx、401／403、500）：工具錯誤，內容為 JSON
  `{"error": {"code", "message", ...服務附帶欄位}, "hint", "http_status"}`，不降級
- 服務不可達（連線、逾時、協定錯誤、502／503／504、Cloudflare 521–524／530）：
  `recall`／`get`／`list`／`vault_resolve` 改讀本地快照（只走 lexical），
  回傳標 `degraded: true`、
  `degraded_reason: "service_unreachable"` 與快照時間；
  `write`／`update`／`upload` 直接失敗、不排離線佇列。
  快照不含文件（T-69）：降級時 recall 的 chunk 列在 `unsupported_kinds`、get 的
  文件／chunk id 列在 `unavailable`、list 的 document 列在 `unsupported_kinds`，
  不以空結果冒充「沒有」
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import anyio
import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from lore_vault import notes as notes_service
from lore_vault.binding import resolve_binding
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.hooks import concept_snapshot
from lore_vault.notes import InvalidCursor, NoChanges
from lore_vault.notes.service import DEFAULT_GET_BUDGET, DEFAULT_LIST_LIMIT
from lore_vault.recall import UnsupportedKind
from lore_vault.recall import recall as recall_service
from lore_vault.recall.service import DEFAULT_BUDGET as RECALL_DEFAULT_BUDGET
from lore_vault.recall.service import DEFAULT_LIMIT as RECALL_DEFAULT_LIMIT
from lore_vault.recall.service import KIND_CHUNK, MODE_LEXICAL
from lore_vault.schema import SPACE_DEV, SPACES, canonical_key
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.errors import (
    InvalidSpace,
    NotFound,
    SpaceKeyPrefixRequired,
    SpaceRequired,
    StorageError,
    UnknownVault,
    VaultRequired,
)
from lore_vault.storage.notes import count_notes
from lore_vault.storage.timeutil import format_utc, parse_utc
from lore_vault.storage.vaults import ALL_VAULTS, get_vault, validate_space

from .client import ServiceClient, ServiceError, ServiceUnreachable
from .settings import ShellSettings
from .snapshot import pull_concepts, pull_snapshot
from .upload import UploadPathError, read_upload

logger = logging.getLogger("lore_vault.mcp")

TOOL_NAMES = (
    "space",
    "vault_resolve",
    "recall",
    "ask",
    "get",
    "list",
    "write",
    "update",
    "upload",
    "status",
)
SPACE_ACTIONS = ("get", "set")
DEGRADED_REASON = "service_unreachable"

INSTRUCTIONS = (
    "Lore Vault：專案記憶。流程：先 vault_resolve 取得本專案的 vault key → recall 查"
    "（只回標題與摘要）→ 需要全文再 get → 新結論用 write、修正既有 note 用 update"
    "（不要另建更正篇）。每次讀寫都要帶 vault；跨 vault 查詢必須明示 vault='*'"
    "（只涵蓋目前 space）。內容分 space：dev（開發記憶，預設）、lore（世界觀）、"
    "personal（私人）；所有工具只看得到目前 space，要看別的 space 先用 "
    "space(action='set') 切換，新 session 一律回到 dev。"
    "回傳 degraded=true 代表服務不可達、結果來自本地快照（可能過時、只有關鍵字檢索、"
    "不含文件）。文件用 upload 上傳後在背景抽取；recall 會一併回文件段落（kind=chunk，"
    "含檔名與 locator 位置），全文用 get 取 doc:／chunk: id。"
    "ask 會把 recall 到的 note 交模型整理成逐點回答；那只是片段的整理、信心有限，"
    "關鍵事實要用 get 核對原 note。"
)

_HINTS = {
    "version_conflict": (
        "note 已被其他寫入更新：先 get 取最新內容確認，再以 current.updated 當 "
        "expected_updated 重試 update"
    ),
    "unknown_vault": (
        "先用 vault_resolve 取得正確的 vault key；此專案確實還沒有 vault 時，"
        "用 vault_resolve(create=True) 建立"
    ),
    "vault_required": "傳入 vault_resolve 回傳的 key；跨 vault 查詢請明示 vault='*'",
    "space_required": "殼應自動帶入目前 space；若直接打 HTTP，請帶 space",
    "invalid_space": f"space 只能是 {sorted(SPACES)} 之一",
    "space_key_prefix_required": (
        "lore／personal 的 vault key 必須以 '<space>/' 開頭，例如 'lore/aeswir-arc'"
    ),
    "invalid_cursor": "cursor 只能用上一頁 list 回傳的 next_cursor 原樣傳回",
    "no_changes": "update 至少要改一個欄位（title／body／topics／links／supersedes）",
    "duplicate": "已有相同 id 的 note；改用 update",
    "path_not_allowed": (
        "只能上傳殼工作目錄或設定 mcp.upload_roots 底下的檔案；"
        "請把檔案放進專案目錄，或請使用者加入白名單"
    ),
    "too_large": "單檔上限 25MB（設定 documents.max_file_bytes）",
    "ask_not_configured": "服務沒有問答模型（缺 OPENAI_API_KEY）；改用 recall + get",
    "ask_provider_error": "問答模型呼叫失敗；稍後重試，或改用 recall + get",
    "ask_timeout": "問答模型逾時；稍後重試、降低 k，或改用 recall + get",
    "ask_rate_limited": "問答模型被限流；等 retry_after 秒後重試，或改用 recall + get",
    "ask_invalid_output": (
        "模型輸出不合格（空、截斷或格式錯）；重試一次，仍失敗改用 recall + get"
    ),
    "unsupported_format": (
        "支援 md、txt（含程式碼等純文字）、json、yaml、toml、pdf、docx、pptx"
    ),
}


AUTHOR_FIELD_DESCRIPTION = (
    "你自己的角色名（例如 Minka；子代理用各自的名稱），單行、最多 64 字。"
    "不可代填別人的名字，也不可填 legacy；省略記為未具名"
)


def _tool_error(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    http_status: int | None = None,
    **extra: Any,
) -> ToolError:
    payload: dict[str, Any] = {"error": {"code": code, "message": message, **extra}}
    if hint:
        payload["hint"] = hint
    if http_status is not None:
        payload["http_status"] = http_status
    return ToolError(json.dumps(payload, ensure_ascii=False))


def _from_service_error(exc: ServiceError) -> ToolError:
    extra: dict[str, Any] = {}
    service_hint = None
    if exc.body and isinstance(exc.body.get("error"), dict):
        extra = {
            k: v
            for k, v in exc.body["error"].items()
            if k not in ("code", "message", "hint")
        }
        service_hint = exc.body["error"].get("hint")
    code = exc.code or f"http_{exc.status}"
    hint = _HINTS.get(code, service_hint if isinstance(service_hint, str) else None)
    return _tool_error(code, exc.message, hint=hint, http_status=exc.status, **extra)


def _from_local_error(exc: Exception) -> ToolError:
    """降級路徑（快照上跑服務層函式）的例外 → 與服務端相同的錯誤碼。"""
    if isinstance(exc, VaultRequired):
        code = "vault_required"
    elif isinstance(exc, SpaceRequired):
        code = "space_required"
    elif isinstance(exc, InvalidSpace):
        code = "invalid_space"
    elif isinstance(exc, SpaceKeyPrefixRequired):
        code = "space_key_prefix_required"
    elif isinstance(exc, UnknownVault):
        return _tool_error(
            "unknown_vault",
            f"{exc}（本地快照中找不到；快照可能早於該 vault 建立）",
            hint=_HINTS["unknown_vault"],
            degraded=True,
        )
    elif isinstance(exc, NotFound):
        code = "not_found"
    elif isinstance(exc, InvalidCursor):
        code = "invalid_cursor"
    elif isinstance(exc, UnsupportedKind):
        code = "unsupported_kind"
    elif isinstance(exc, NoChanges):
        code = "no_changes"
    elif isinstance(exc, StorageError):
        code = "storage_error"
    else:  # ValueError／TypeError：參數驗證
        code = "invalid_request"
    return _tool_error(code, str(exc), hint=_HINTS.get(code), degraded=True)


def _compact(**values: Any) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:299] + "…"


def _vault_payload(conn: sqlite3.Connection, key: str, space: str) -> dict[str, Any]:
    """與 `/v1/vault_resolve` 相同形狀（降級時由快照產生，同樣限定在 space 內）。"""
    if key == ALL_VAULTS:
        raise VaultRequired("此操作必須指定單一 vault，不可用 '*'")
    vault = get_vault(conn, key, space=space)
    return {
        "key": vault.key,
        "display": vault.display,
        "kind": vault.kind,
        "space": vault.space,
        "aliases": list(vault.aliases),
        "note_count": count_notes(conn, vault.key, space=vault.space),
        "requested": key,
        "via_alias": canonical_key(key) != vault.key,
    }


class Shell:
    """工具邏輯（與 MCP 註冊分開，方便測試）。"""

    def __init__(
        self,
        settings: ShellSettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        cwd: Callable[[], str] = os.getcwd,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.client = ServiceClient(settings, transport=transport)
        self._cwd = cwd
        self._now = now
        self._pull_lock = anyio.Lock()
        self.last_pull_error: str | None = None
        self._concept_lock = anyio.Lock()
        self.last_concept_pull_error: str | None = None
        # 目前 space：只在記憶體，不持久化（新殼行程一律 dev）
        self.space: str = SPACE_DEV

    async def aclose(self) -> None:
        await self.client.aclose()

    # ── 快照 ──

    async def refresh_snapshot(self) -> storage_snapshot.Manifest | None:
        """拉一次快照；失敗只記 log 與 `last_pull_error`，不影響工具。"""
        snapshot_dir = self.settings.snapshot_dir
        if snapshot_dir is None:
            return None
        async with self._pull_lock:
            try:
                manifest = await pull_snapshot(self.client, snapshot_dir)
            except (
                ServiceUnreachable,
                ServiceError,
                storage_snapshot.SnapshotError,
                OSError,
            ) as exc:
                self.last_pull_error = _describe(exc)
                logger.warning("拉取快照失敗：%s", self.last_pull_error)
                return None
        self.last_pull_error = None
        logger.info(
            "快照已更新：%s（%d 則 note）", manifest.generated_at, manifest.notes
        )
        return manifest

    async def refresh_concepts(self) -> concept_snapshot.ConceptManifest | None:
        """拉一次 concept 快照（PreToolUse 用）；失敗只記 log，
        不影響工具與 notes 快照。"""
        path = self.settings.concept_snapshot_path
        if path is None:
            return None
        async with self._concept_lock:
            try:
                manifest = await pull_concepts(self.client, path)
            except (
                ServiceUnreachable,
                ServiceError,
                concept_snapshot.ConceptSnapshotError,
                OSError,
            ) as exc:
                self.last_concept_pull_error = _describe(exc)
                logger.warning(
                    "拉取 concept 快照失敗：%s", self.last_concept_pull_error
                )
                return None
        self.last_concept_pull_error = None
        logger.info("concept 快照已更新：%d 條", manifest.concepts)
        return manifest

    async def snapshot_loop(self) -> None:
        while True:
            await self.refresh_snapshot()
            await self.refresh_concepts()
            if self.settings.snapshot_interval <= 0:
                return
            await anyio.sleep(self.settings.snapshot_interval)

    def _snapshot_meta(self, manifest: storage_snapshot.Manifest) -> dict[str, Any]:
        # age：距最近一次向服務確認快照仍是最新（下載或 304）的秒數
        age = (self._now() - parse_utc(manifest.fresh_as_of)).total_seconds()
        return {
            "generated_at": manifest.generated_at,
            "checked_at": manifest.fresh_as_of,
            "age_seconds": max(0, int(age)),
        }

    def _degraded(
        self,
        cause: ServiceUnreachable,
        query: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot_dir = self.settings.snapshot_dir
        if snapshot_dir is None:
            raise _tool_error(
                DEGRADED_REASON,
                f"服務不可達（{cause.detail}），且未設定快照目錄 mcp.snapshot_dir，"
                "無法降級讀取",
            )
        try:
            conn, manifest = storage_snapshot.open_snapshot(snapshot_dir)
        except (StorageError, OSError, sqlite3.Error) as exc:
            raise _tool_error(
                DEGRADED_REASON,
                f"服務不可達（{cause.detail}），本地快照也無法使用：{exc}",
            ) from None
        try:
            result = query(conn)
        except (StorageError, ValueError, TypeError) as exc:
            raise _from_local_error(exc) from None
        finally:
            # 立刻關閉：Windows 上開著的連線會讓下一次快照替換失敗
            conn.close()
        result["degraded"] = True
        result["degraded_reason"] = DEGRADED_REASON
        result["degraded_detail"] = cause.detail
        result["snapshot"] = self._snapshot_meta(manifest)
        return result

    async def _send(
        self,
        path: str,
        body: dict[str, Any],
        *,
        space: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """所有 `/v1/*` 請求的唯一出口：注入 space（預設為目前 space）。"""
        return await self.client.post(
            path, {**body, "space": space or self.space}, timeout=timeout
        )

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._send(path, body)
        except ServiceError as exc:
            raise _from_service_error(exc) from None

    def _write_unreachable(self, cause: ServiceUnreachable) -> ToolError:
        return _tool_error(
            DEGRADED_REASON,
            f"服務不可達（{cause.detail}），寫入未執行；不會排入離線佇列",
            hint=(
                "服務恢復後重試。若是逾時，寫入可能已在服務端完成："
                "重試前先用 list／recall 確認，避免重複"
            ),
        )

    # ── 工具 ──

    def space_tool(self, action: str, value: str | None = None) -> dict[str, Any]:
        """查詢／切換目前 space（純殼端狀態，不打服務、不持久化）。"""
        if action == "get":
            return {"space": self.space, "spaces": sorted(SPACES)}
        if action != "set":
            raise _tool_error(
                "invalid_request",
                f"action 必須是 {list(SPACE_ACTIONS)}，得到 {action!r}",
            )
        try:
            self.space = validate_space(value)
        except (SpaceRequired, InvalidSpace) as exc:
            code = (
                "space_required" if isinstance(exc, SpaceRequired) else "invalid_space"
            )
            raise _tool_error(code, str(exc), hint=_HINTS["invalid_space"]) from None
        return {"space": self.space, "spaces": sorted(SPACES)}

    async def vault_resolve(
        self,
        cwd: str | None = None,
        create: bool = False,
        display: str | None = None,
        space: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        """dev：key 省略時由 cwd 的 binding 算；lore／personal：必須帶 key、忽略 cwd。

        `space` 省略用目前 space；顯式傳入只影響這一次（不切換目前 space）。
        """
        try:
            target = self.space if space is None else validate_space(space)
        except (SpaceRequired, InvalidSpace) as exc:
            raise _tool_error(
                "invalid_space", str(exc), hint=_HINTS["invalid_space"]
            ) from None
        bind: dict[str, Any] | None = None
        cwd_ignored = False
        if key is not None:
            resolved_key, default_display = key, key
            cwd_ignored = cwd is not None
        elif target != SPACE_DEV:
            raise _tool_error(
                "key_required",
                f"space {target!r} 的 vault 沒有 repo 可推算，必須帶 key",
                hint=(
                    f"例如 vault_resolve(key='{target}/<名稱>', create=True, "
                    "display=...)"
                ),
            )
        else:
            path = cwd or self._cwd()
            try:
                binding = resolve_binding(path)
            except NotADirectoryError as exc:
                raise _tool_error("invalid_cwd", str(exc)) from None
            bind = {
                "key": binding.key,
                "display": binding.display,
                "source": binding.source,
                "cwd": str(Path(os.path.abspath(path))),
            }
            resolved_key, default_display = binding.key, binding.display
        try:
            result = await self._send(
                "/v1/vault_resolve", {"key": resolved_key}, space=target
            )
            result["created"] = False
        except ServiceError as exc:
            if exc.code != "unknown_vault":
                raise _from_service_error(exc) from None
            if not create:
                raise _tool_error(
                    "unknown_vault",
                    f"space {target!r} 內的 vault {resolved_key!r} 尚未建立",
                    hint=(
                        "確認這是要記錄的範圍後，以 vault_resolve(create=True, "
                        "display=...) 建立；若 cwd 不是專案目錄，改傳正確的 cwd；"
                        "若 vault 在別的 space，先用 space(action='set') 切換"
                    ),
                    http_status=exc.status,
                    **_compact(binding=bind),
                ) from None
            result = await self._create_vault(
                resolved_key, display or default_display, target
            )
        except ServiceUnreachable as exc:
            # 已存在的 vault 可從快照解析；建立必須等服務恢復
            try:
                result = self._degraded(
                    exc, lambda c: _vault_payload(c, resolved_key, target)
                )
            except ToolError:
                if create:
                    raise self._write_unreachable(exc) from None
                raise
            result["created"] = False
        if bind is not None:
            result["binding"] = bind
        if cwd_ignored:
            result["cwd_ignored"] = True
        return result

    async def _create_vault(self, key: str, display: str, space: str) -> dict[str, Any]:
        try:
            result = await self._send(
                "/v1/vaults", {"key": key, "display": display}, space=space
            )
        except ServiceError as exc:
            if exc.code == "vault_exists" and exc.body:
                # 並行建立：別人剛建好，視同解析成功
                existing = dict(exc.body["error"].get("existing") or {})
                existing["created"] = False
                return existing
            raise _from_service_error(exc) from None
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None
        result["created"] = True
        return result

    async def recall(
        self,
        query: str,
        vault: str,
        kinds: list[str] | None = None,
        limit: int | None = None,
        budget: int | None = None,
    ) -> dict[str, Any]:
        body = _compact(
            query=query, vault=vault, kinds=kinds, limit=limit, budget=budget
        )
        try:
            return await self._post("/v1/recall", body)
        except ServiceUnreachable as exc:
            return self._degraded(
                exc,
                lambda conn: recall_service(
                    conn,
                    query,
                    vault,
                    space=self.space,
                    kinds=kinds,
                    limit=RECALL_DEFAULT_LIMIT if limit is None else limit,
                    budget=RECALL_DEFAULT_BUDGET if budget is None else budget,
                    mode=MODE_LEXICAL,
                    # 快照不含文件：chunk 明確列進 unsupported_kinds（T-69）
                    unavailable_kinds=(KIND_CHUNK,),
                ).to_dict(),
            )

    async def ask(
        self,
        question: str,
        vault: str,
        kinds: list[str] | None = None,
        k: int | None = None,
    ) -> dict[str, Any]:
        body = _compact(question=question, vault=vault, kinds=kinds, k=k)
        try:
            return await self._send("/v1/ask", body, timeout=self.settings.ask_timeout)
        except ServiceError as exc:
            raise _from_service_error(exc) from None
        except ServiceUnreachable as exc:
            # 快照沒有模型：不降級，明確失敗
            raise _tool_error(
                DEGRADED_REASON,
                f"服務不可達（{exc.detail}），ask 需要服務端模型、無法降級",
                hint="服務恢復後重試；或改用 recall（可讀本地快照）+ get",
            ) from None

    async def get(
        self, vault: str, ids: list[str], budget: int | None = None
    ) -> dict[str, Any]:
        try:
            return await self._post(
                "/v1/get", _compact(vault=vault, ids=ids, budget=budget)
            )
        except ServiceUnreachable as exc:
            return self._degraded(
                exc,
                lambda conn: notes_service.get(
                    conn,
                    vault,
                    ids,
                    space=self.space,
                    budget=DEFAULT_GET_BUDGET if budget is None else budget,
                    documents_available=False,
                ).to_dict(),
            )

    async def list_(
        self,
        vault: str,
        since: str | None = None,
        topics: list[str] | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        body = _compact(
            vault=vault,
            since=since,
            topics=topics,
            cursor=cursor,
            limit=limit,
            kinds=kinds,
        )
        try:
            return await self._post("/v1/list", body)
        except ServiceUnreachable as exc:
            return self._degraded(
                exc,
                lambda conn: notes_service.list_(
                    conn,
                    vault,
                    space=self.space,
                    since=since,
                    topics=topics,
                    cursor=cursor,
                    limit=DEFAULT_LIST_LIMIT if limit is None else limit,
                    kinds=kinds,
                    documents_available=False,
                ).to_dict(),
            )

    async def write(
        self,
        vault: str,
        title: str,
        body: str,
        topics: list[str] | None = None,
        links: list[str] | None = None,
        supersedes: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        # principal 不從殼送：服務依憑證判定（A22）
        payload = _compact(
            vault=vault,
            title=title,
            body=body,
            topics=topics,
            links=links,
            supersedes=supersedes,
            author=author,
        )
        try:
            return await self._post("/v1/write", payload)
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None

    async def update(
        self,
        vault: str,
        id: str,
        expected_updated: str,
        title: str | None = None,
        body: str | None = None,
        topics: list[str] | None = None,
        links: list[str] | None = None,
        supersedes: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        payload = _compact(
            vault=vault,
            id=id,
            expected_updated=expected_updated,
            title=title,
            body=body,
            topics=topics,
            links=links,
            author=author,
        )
        if supersedes is not None:
            # 空字串 = 清除更正關係（服務端以 null 表示）
            payload["supersedes"] = supersedes or None
        try:
            return await self._post("/v1/update", payload)
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None

    def upload_roots(self) -> tuple[Path, ...]:
        """可上傳的目錄：殼的工作目錄一律在內，另加設定的 `mcp.upload_roots`。"""
        return (Path(self._cwd()), *self.settings.upload_roots)

    async def upload(self, path: str, vault: str | None = None) -> dict[str, Any]:
        """讀本機檔案上傳到目前 space 的 vault。

        `vault` 省略時只在 dev 以殼工作目錄的 binding 解析（不建立 vault），並在回應
        標 `vault_source: "cwd_binding"`；lore／personal 必須明示。
        """
        try:
            local = read_upload(
                path,
                self.upload_roots(),
                cwd=self._cwd(),
                max_bytes=self.settings.max_upload_bytes,
            )
        except UploadPathError as exc:
            raise _tool_error(exc.code, str(exc), hint=_HINTS.get(exc.code)) from None
        vault_source = "explicit"
        if vault is None:
            if self.space != SPACE_DEV:
                raise _tool_error(
                    "vault_required",
                    f"space {self.space!r} 沒有 repo 可推算，upload 必須帶 vault",
                    hint=_HINTS["vault_required"],
                )
            try:
                binding = resolve_binding(self._cwd())
            except NotADirectoryError as exc:
                raise _tool_error("invalid_cwd", str(exc)) from None
            vault, vault_source = binding.key, "cwd_binding"
        try:
            result = await self.client.post_multipart(
                "/v1/documents",
                {"vault": vault, "space": self.space},
                filename=local.name,
                content=local.data,
            )
        except ServiceError as exc:
            raise _from_service_error(exc) from None
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None
        result["vault_source"] = vault_source
        result["path"] = str(local.path)
        return result

    def local_status(self) -> dict[str, Any]:
        """殼端可獨立判斷的狀態：快照對帳（不需要服務）。"""
        settings: dict[str, Any] = {
            "snapshot_max_age_hours": self.settings.snapshot_max_age_hours,
            "now": self._now(),
        }
        if self.settings.snapshot_dir is not None:
            settings["snapshot_dir"] = str(self.settings.snapshot_dir)
        report = default_registry().run(
            DoctorContext(settings=settings), categories=["snapshot"]
        )
        return {
            "ok": report.ok,
            "space": self.space,
            "base_url": self.settings.base_url,
            "snapshot_dir": (
                str(self.settings.snapshot_dir) if self.settings.snapshot_dir else None
            ),
            "last_pull_error": self.last_pull_error,
            "concept_snapshot_path": (
                str(self.settings.concept_snapshot_path)
                if self.settings.concept_snapshot_path
                else None
            ),
            "last_concept_pull_error": self.last_concept_pull_error,
            "doctor": report.to_dict(),
        }

    async def status(self, vault: str | None = None) -> dict[str, Any]:
        local = self.local_status()
        try:
            result = await self._post("/v1/status", _compact(vault=vault))
        except ServiceUnreachable as exc:
            return {
                "ok": False,
                "degraded": True,
                "degraded_reason": DEGRADED_REASON,
                "degraded_detail": exc.detail,
                "checked_at": format_utc(self._now()),
                "service": {"base_url": self.settings.base_url, "reachable": False},
                "shell": local,
            }
        result["shell"] = local
        return result


# ── MCP 註冊 ────────────────────────────────────────────────────────

VaultArg = Annotated[
    str,
    Field(description="vault key（vault_resolve 回傳的 key）；跨 vault 查詢明示 '*'"),
]


def _dump(result: dict[str, Any]) -> str:
    # 緊湊 JSON：agent 的上下文是預算，不縮排
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def build_server(shell: Shell) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        try:
            async with anyio.create_task_group() as tg:
                if shell.settings.snapshot_on_start and (
                    shell.settings.snapshot_dir or shell.settings.concept_snapshot_path
                ):
                    tg.start_soon(shell.snapshot_loop)
                try:
                    yield None
                finally:
                    tg.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await shell.aclose()

    server = MCPServer(
        name="lore-vault",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
    )

    async def space(
        action: Annotated[
            str, Field(description="'get' 查詢目前 space；'set' 切換到 value")
        ],
        value: Annotated[
            str | None,
            Field(description="action='set' 時的目標：'dev'、'lore' 或 'personal'"),
        ] = None,
    ) -> str:
        return _dump(shell.space_tool(action, value))

    async def vault_resolve(
        cwd: Annotated[
            str | None,
            Field(
                description="專案目錄（只用於 dev）；省略時用殼啟動時的工作目錄"
                "（通常是專案根）"
            ),
        ] = None,
        create: Annotated[
            bool,
            Field(description="vault 不存在時建立；只在確認要為此專案建記憶時設 true"),
        ] = False,
        display: Annotated[
            str | None,
            Field(description="建立時的顯示名稱；省略時用 repo 名（或 key）"),
        ] = None,
        space: Annotated[
            str | None,
            Field(
                description="要在哪個 space 解析／建立；省略用目前 space。"
                "顯式傳入只影響這一次，不切換目前 space"
            ),
        ] = None,
        key: Annotated[
            str | None,
            Field(
                description="直接指定 vault key（不經 cwd 推算）。lore／personal 必填，"
                "且必須以 '<space>/' 開頭，如 'lore/aeswir-arc'"
            ),
        ] = None,
    ) -> str:
        return _dump(await shell.vault_resolve(cwd, create, display, space, key))

    async def recall(
        query: Annotated[str, Field(description="查詢詞（中英文、識別字皆可）")],
        vault: VaultArg,
        kinds: Annotated[
            list[str] | None,
            Field(
                description="要查的種類：'note'、'chunk'（文件段落），預設兩者；"
                "concept 尚未支援"
            ),
        ] = None,
        limit: Annotated[
            int | None, Field(description="最多幾筆，預設 10、上限 100")
        ] = None,
        budget: Annotated[
            int | None,
            Field(description="所有結果 title+summary 字數總和上限，預設 2000"),
        ] = None,
    ) -> str:
        return _dump(await shell.recall(query, vault, kinds, limit, budget))

    async def ask(
        question: Annotated[str, Field(description="要問的問題（自然語言）")],
        vault: VaultArg,
        kinds: Annotated[
            list[str] | None,
            Field(
                description="檢索種類，預設 ['note']；目前只支援 note，"
                "文件段落（chunk）的問答另行評估"
            ),
        ] = None,
        k: Annotated[
            int | None,
            Field(description="取前幾則 note 當片段，預設 10、上限 20"),
        ] = None,
    ) -> str:
        return _dump(await shell.ask(question, vault, kinds, k))

    async def get(
        vault: VaultArg,
        ids: Annotated[
            list[str],
            Field(
                description="note id、文件 id（doc:…，取整份）或 chunk id（chunk:…，"
                "取該段），一次最多 50"
            ),
        ],
        budget: Annotated[
            int | None,
            Field(description="所有 body 字數總和上限，預設 12000；超過的截斷並標示"),
        ] = None,
    ) -> str:
        return _dump(await shell.get(vault, ids, budget))

    async def list_(
        vault: VaultArg,
        since: Annotated[
            str | None, Field(description="只列此時間之後更新的（ISO-8601 UTC）")
        ] = None,
        topics: Annotated[
            list[str] | None, Field(description="只列含這些 topic 的")
        ] = None,
        cursor: Annotated[
            str | None, Field(description="上一頁回傳的 next_cursor")
        ] = None,
        limit: Annotated[
            int | None, Field(description="每頁筆數，預設 50、上限 200")
        ] = None,
        kinds: Annotated[
            list[str] | None,
            Field(description="'note'／'document'，預設兩者；指定 topics 時只列 note"),
        ] = None,
    ) -> str:
        return _dump(await shell.list_(vault, since, topics, cursor, limit, kinds))

    async def write(
        vault: Annotated[str, Field(description="vault key（單一 vault，不可 '*'）")],
        title: Annotated[str, Field(description="標題（查詢結果主要呈現）")],
        body: Annotated[str, Field(description="markdown 全文")],
        topics: Annotated[list[str] | None, Field(description="標籤")] = None,
        links: Annotated[
            list[str] | None, Field(description="相關 note 的標題或 id")
        ] = None,
        supersedes: Annotated[
            str | None, Field(description="此 note 取代的舊 note id")
        ] = None,
        author: Annotated[
            str | None,
            Field(description=AUTHOR_FIELD_DESCRIPTION),
        ] = None,
    ) -> str:
        return _dump(
            await shell.write(
                vault, title, body, topics, links, supersedes, author=author
            )
        )

    async def update(
        vault: Annotated[str, Field(description="vault key（單一 vault）")],
        id: Annotated[str, Field(description="note id")],
        expected_updated: Annotated[
            str,
            Field(description="讀到的 updated 值；版本不符回衝突並附目前版本"),
        ],
        title: Annotated[str | None, Field(description="新標題")] = None,
        body: Annotated[str | None, Field(description="新全文（會重算摘要）")] = None,
        topics: Annotated[
            list[str] | None, Field(description="新標籤（整批替換）")
        ] = None,
        links: Annotated[
            list[str] | None, Field(description="新連結（整批替換）")
        ] = None,
        supersedes: Annotated[
            str | None, Field(description="取代的舊 note id；空字串 = 清除")
        ] = None,
        author: Annotated[
            str | None,
            Field(description=AUTHOR_FIELD_DESCRIPTION + "（記為最後修改者）"),
        ] = None,
    ) -> str:
        return _dump(
            await shell.update(
                vault,
                id,
                expected_updated,
                title,
                body,
                topics,
                links,
                supersedes,
                author=author,
            )
        )

    async def upload(
        path: Annotated[
            str,
            Field(
                description="本機檔案路徑（絕對，或相對於殼的工作目錄）；必須在殼工作"
                "目錄或 mcp.upload_roots 之下，不可含 '..'"
            ),
        ],
        vault: Annotated[
            str | None,
            Field(
                description="vault key（單一 vault）。dev 省略時用殼工作目錄 binding；"
                "lore／personal 必填"
            ),
        ] = None,
    ) -> str:
        return _dump(await shell.upload(path, vault))

    async def status(
        vault: Annotated[
            str | None, Field(description="另附該 vault 的筆數與最近更新")
        ] = None,
    ) -> str:
        return _dump(await shell.status(vault))

    descriptions = {
        "space": (
            "查詢或切換「目前 space」：dev（開發記憶，預設）、lore（世界觀）、"
            "personal（私人）。其他工具只看得到目前 space 的內容（vault='*' 也只涵蓋"
            "目前 space）。只在本殼行程的記憶體中，新 session 一律回到 dev。"
        ),
        "vault_resolve": (
            "取得 vault key。dev：由 cwd 的 git remote 算出（自動解析改名別名）；"
            "lore／personal：沒有 repo，必須帶 key（'<space>/名稱'），cwd 會被忽略。"
            "每個 session 開始時先呼叫一次，之後所有工具都帶回傳的 key。"
            "vault 不存在會回錯誤；確認要建記憶時才用 create=true 建立。"
        ),
        "recall": (
            "在 vault 內檢索記憶與已上傳文件（關鍵字 + 語意）。note 只回 id、標題、"
            "1–2 句摘要；文件段落（kind=chunk）回檔名、locator（頁／投影片／標題）與"
            "片段摘錄。不含全文；字數受 budget 限制，被裁掉的筆數見 omitted。"
            "需要全文時再用 get。degraded=true 表示服務不可達、結果來自本地快照"
            "（不含文件，chunk 列在 unsupported_kinds）。"
        ),
        "ask": (
            "用 recall 的同一條檢索取前 k 則 note，交模型整理成逐點回答"
            "（answer.points，每點附 note_ids）。注意：回答只是檢索片段的整理，"
            "信心有限——片段沒撈到的不會知道；關鍵事實請以 get 核對原 note 再採用。"
            "status=insufficient 表示片段不足；unsupported=true 的點沒有有效引用"
            "（引用了片段外的 id 已移除，見 dropped_citations），不要當成事實。"
            "degraded=true 表示檢索降級（只走關鍵字）。目前只用 note，不含文件。"
            "服務不可達時直接失敗（沒有快照降級）。"
        ),
        "get": (
            "依 id 批次取全文：note id 回 body；doc:… 回整份文件文字；chunk:… 回該段。"
            "字數總和受 budget 限制（依 ids 順序分配），超過的標 truncated；"
            "不在該 vault 的 id 列在 missing。"
        ),
        "list": (
            "列出 vault 的 note 標題與文件（新到舊，分頁；文件含 status、error_code、"
            "version、superseded_by）。用來瀏覽或確認近期寫入與文件抽取狀態；"
            "找特定主題請用 recall。has_more=true 時用 next_cursor 取下一頁。"
            "note 摘要受字數預算（預設 4000）限制，在本頁平均分配：過長的被截短"
            "（結尾「…」、項目標 summary_truncated=true），預算連下限都給不起的尾端"
            "note 摘要省略（summary_source=omitted）；要完整內容用 get 取該 id 的全文。"
        ),
        "write": (
            "寫入新的 note。回傳 id 與疑似重複清單（duplicates）；若與既有 note 重複，"
            "改用 update 修正原 note，不要另建更正篇。author 請填你自己的角色名"
            "（例如 Minka；子代理用各自的名稱），不要填別人的名字；不確定就省略"
            "（記為未具名）。服務不可達時直接失敗。"
        ),
        "update": (
            "修改既有 note；必須帶讀到的 expected_updated。版本衝突時錯誤內附 "
            "current（目前版本），確認後以 current.updated 重試。author 填你自己的"
            "角色名（記為最後修改者，原作者不變）。服務不可達時直接失敗。"
        ),
        "upload": (
            "上傳本機文件（md、txt、程式碼、json、yaml、toml、pdf、docx、pptx；單檔 "
            "25MB）到目前 space 的 vault。回 document_id 與 status（pending：背景抽取"
            "中）；同內容再傳回 duplicate=true；同檔名不同內容為新版本（supersedes）。"
            "只能讀殼工作目錄或 mcp.upload_roots 之下的檔案。服務不可達時直接失敗。"
        ),
        "status": (
            "服務健康狀態（doctor 對帳、補算積壓、schema 版本）與本地快照狀態。"
            "服務不可達時仍回傳殼端狀態並標 degraded。"
        ),
    }
    for name, fn in (
        ("space", space),
        ("vault_resolve", vault_resolve),
        ("recall", recall),
        ("ask", ask),
        ("get", get),
        ("list", list_),
        ("write", write),
        ("update", update),
        ("upload", upload),
        ("status", status),
    ):
        # 非結構化輸出：只回一份緊湊 JSON 文字，不重複送 structuredContent
        server.add_tool(
            fn, name=name, description=descriptions[name], structured_output=False
        )
    return server
