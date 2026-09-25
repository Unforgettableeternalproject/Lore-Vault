"""`/v1` 端點：七個 MCP 工具一對一的 RPC 式 HTTP 契約，外加建 vault。

薄轉接：參數原樣交給服務層（驗證、vault 硬範圍、預算都在服務層），
回應直接用服務層的 `to_dict()`。每個請求在同一執行緒內開、用、關自己的連線。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field

from lore_vault import notes as notes_service
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.doctor.builtin import DEFAULT_BACKLOG_MAX_AGE
from lore_vault.notes.service import DEFAULT_GET_BUDGET, DEFAULT_LIST_LIMIT
from lore_vault.recall import recall as recall_service
from lore_vault.recall.service import DEFAULT_BUDGET as RECALL_DEFAULT_BUDGET
from lore_vault.recall.service import DEFAULT_LIMIT as RECALL_DEFAULT_LIMIT
from lore_vault.recall.service import MODE_HYBRID
from lore_vault.schema import Vault, canonical_key
from lore_vault.storage import enrichment as storage_enrichment
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import UnknownVault, VaultRequired
from lore_vault.storage.migrate import SCHEMA_VERSION, current_version
from lore_vault.storage.notes import count_notes, list_notes
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import ALL_VAULTS, get_vault, upsert_vault

from .errors import VaultExists
from .state import AppState

router = APIRouter(prefix="/v1")


def _state(request: Request) -> AppState:
    return request.app.state.lore


# ── 請求模型：未知欄位一律拒絕（打錯參數名不能被默默忽略）──
# vault 宣告成可省略：缺值交給服務層拋 VaultRequired（400），不讓它變成 422。


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveRequest(_Req):
    key: str = Field(description="客戶端以 lore_vault.binding 算出的 binding key")


class CreateVaultRequest(_Req):
    key: str
    display: str
    kind: str = "repo"
    aliases: list[str] = Field(default_factory=list)


class RecallRequest(_Req):
    query: str
    vault: str | None = None
    kinds: list[str] | None = None
    limit: int = RECALL_DEFAULT_LIMIT
    budget: int = RECALL_DEFAULT_BUDGET
    mode: str = MODE_HYBRID


class GetRequest(_Req):
    vault: str | None = None
    ids: list[str]
    budget: int = DEFAULT_GET_BUDGET


class ListRequest(_Req):
    vault: str | None = None
    since: str | None = None
    topics: list[str] | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIST_LIMIT


class WriteRequest(_Req):
    vault: str | None = None
    title: str
    body: str
    topics: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    supersedes: str | None = None


class UpdateRequest(_Req):
    vault: str | None = None
    id: str
    expected_updated: str
    title: str | None = None
    body: str | None = None
    topics: list[str] | None = None
    links: list[str] | None = None
    # 有傳（含 null）才更新；null 代表清除更正關係
    supersedes: str | None = None


class StatusRequest(_Req):
    vault: str | None = None


# ── vault ──


def _vault_dict(conn: sqlite3.Connection, vault: Vault) -> dict[str, Any]:
    return {
        "key": vault.key,
        "display": vault.display,
        "kind": vault.kind,
        "aliases": list(vault.aliases),
        "note_count": count_notes(conn, vault.key),
    }


def _single_vault(conn: sqlite3.Connection, key: str | None) -> Vault:
    if key == ALL_VAULTS:
        raise VaultRequired("此操作必須指定單一 vault，不可用 '*'")
    return get_vault(conn, key)  # type: ignore[arg-type]  # None → VaultRequired


@router.post("/vault_resolve")
def vault_resolve(request: Request, req: ResolveRequest) -> dict[str, Any]:
    """binding key（或別名）→ 現行 vault。服務端讀不到客戶端 cwd，key 由客戶端算。"""
    with _state(request).connection() as conn:
        vault = _single_vault(conn, req.key)
        result = _vault_dict(conn, vault)
    result["requested"] = req.key
    result["via_alias"] = canonical_key(req.key) != vault.key
    return result


@router.post("/vaults", status_code=status.HTTP_201_CREATED)
def create_vault(request: Request, req: CreateVaultRequest) -> dict[str, Any]:
    """明確建立 vault（只新增：key 或別名已存在回 409，不覆寫既有別名）。"""
    if req.key.strip() == ALL_VAULTS or ALL_VAULTS in req.aliases:
        raise VaultRequired("'*' 保留給跨 vault 查詢，不可當 vault key 或別名")
    vault = Vault(
        key=req.key, display=req.display, kind=req.kind, aliases=tuple(req.aliases)
    )
    with _state(request).connection() as conn:
        with transaction(conn):
            for candidate in (vault.key, *vault.aliases):
                try:
                    existing = get_vault(conn, candidate)
                except UnknownVault:
                    continue
                raise VaultExists(
                    f"{candidate!r} 已存在（屬於 vault {existing.key!r}）",
                    _vault_dict(conn, existing),
                )
            upsert_vault(conn, vault)
        return _vault_dict(conn, get_vault(conn, vault.key))


# ── 七個工具 ──


@router.post("/recall")
def recall(request: Request, req: RecallRequest) -> dict[str, Any]:
    state = _state(request)
    with state.connection() as conn:
        result = recall_service(
            conn,
            req.query,
            req.vault,  # type: ignore[arg-type]
            embedder=state.query_embedder,
            dim=state.dim,
            kinds=req.kinds,
            limit=req.limit,
            budget=req.budget,
            mode=req.mode,
        )
    return result.to_dict()


@router.post("/get")
def get(request: Request, req: GetRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        result = notes_service.get(conn, req.vault, req.ids, budget=req.budget)  # type: ignore[arg-type]
    return result.to_dict()


@router.post("/list")
def list_(request: Request, req: ListRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        result = notes_service.list_(
            conn,
            req.vault,  # type: ignore[arg-type]
            since=req.since,
            topics=req.topics,
            cursor=req.cursor,
            limit=req.limit,
        )
    return result.to_dict()


@router.post("/write", status_code=status.HTTP_201_CREATED)
def write(request: Request, req: WriteRequest) -> dict[str, Any]:
    state = _state(request)
    with state.connection() as conn:
        result = notes_service.write(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.title,
            req.body,
            topics=req.topics,
            links=req.links,
            supersedes=req.supersedes,
            embedder=state.query_embedder,
            dim=state.dim,
        )
    state.wake_worker()
    return result.to_dict()


@router.post("/update")
def update(request: Request, req: UpdateRequest) -> dict[str, Any]:
    state = _state(request)
    extra: dict[str, Any] = {}
    if "supersedes" in req.model_fields_set:
        extra["supersedes"] = req.supersedes
    with state.connection() as conn:
        result = notes_service.update(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            req.expected_updated,
            title=req.title,
            body=req.body,
            topics=req.topics,
            links=req.links,
            **extra,
        )
    if result.summary_stale or result.embedding_stale:
        state.wake_worker()
    return result.to_dict()


@router.post("/status")
def status_(
    request: Request, req: Annotated[StatusRequest | None, Body()] = None
) -> dict[str, Any]:
    """doctor 全部檢查項 + 補算積壓與 worker 狀態 + schema 版本（T-25）。

    doctor 是全域對帳（不分 vault）；指定 vault 時另附該 vault 的筆數與最近更新。
    """
    state = _state(request)
    vault_key = req.vault if req is not None else None
    now = datetime.now(UTC)
    with state.connection() as conn:
        vault_info = None
        if vault_key is not None:
            vault = _single_vault(conn, vault_key)
            vault_info = _vault_dict(conn, vault)
            latest, _ = list_notes(conn, vault.key, limit=1)
            vault_info["last_updated"] = latest[0].updated if latest else None
        report = default_registry().run(
            DoctorContext(
                settings={
                    "embedding_dim": state.dim,
                    "now": now,
                    "enrich_backlog_max_age": DEFAULT_BACKLOG_MAX_AGE,
                    # 未設定備份目錄時 backup.recent 記為 skipped
                    "backup_dir": state.settings.config.backup.dir,
                    "backup_max_age_hours": state.settings.config.backup.max_age_hours,
                },
                resources={"db": conn},
            )
        )
        backlog = storage_enrichment.enrichment_backlog(
            conn, now=now, max_age_seconds=DEFAULT_BACKLOG_MAX_AGE
        )
        schema = {"version": current_version(conn), "expected": SCHEMA_VERSION}
    worker = state.worker_status()
    return {
        # 同程序的 worker 起不來（fatal_error）也算不健康
        "ok": report.ok and not worker.get("fatal_error"),
        "checked_at": format_utc(now),
        "schema": schema,
        "vault": vault_info,
        "enrich": {
            "worker": worker,
            "backlog": {
                "status": backlog.status,
                "summary": backlog.summary,
                "counts": dict(backlog.counts),
            },
        },
        "doctor": report.to_dict(),
    }
