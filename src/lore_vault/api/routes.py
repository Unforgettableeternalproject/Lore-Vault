"""`/v1` 端點：MCP 工具一對一的 RPC 式 HTTP 契約，外加建 vault 與唯讀快照。

薄轉接：參數原樣交給服務層（驗證、vault／space 硬範圍、預算都在服務層），
回應直接用服務層的 `to_dict()`。每個請求在同一執行緒內開、用、關自己的連線。

`space`（A18）：每個讀寫請求必填、無預設，缺少回 400 `space_required`
（服務端無狀態；「目前 space」由 MCP 殼持有並注入）。例外只有無 body 的
`POST /v1/status`（純健康檢查）與 `GET /v1/snapshot`（整庫唯讀副本，由殼端依
目前 space 過濾）。

作者（A22）：`write`／`update` 接受 `author`（寫入者自報名，未填存 null、不代填）；
`principal` 由認證中介層依憑證判定（`api.principals`），body 帶 `principal` 與其他
未知欄位一樣 422 拒絕。

`POST /v1/ask`（D11）：recall 同一條檢索取 note 片段，交問答模型整理；模型失敗回
明確錯誤碼（`ask_*`，見 `api.errors`），不回假成功。查詢 embedding 一律用
`embedding.cold_query_timeout`（recall 在模型已載入時用短逾時）。

`POST /v1/documents`（T-67）是唯一的 multipart 端點：欄位 `file`、`vault`、`space`
（必填）、`filename?`、`mime?`；大小上限在讀取 body 時就擋（413 `too_large`）。

`POST /v1/blob_put`／`POST /v1/blob_get`（schema v17，v18 加版本）：通用、不檢索的小型
機器狀態側載（`storage.sidecar`），以 `(vault, key)` 單列覆寫、每次 put 版本遞增；不進
FTS／embedding／recall／list／snapshot，不提供 MCP 工具。內容以 `content_base64` 傳遞，
解碼後上限依 key 前綴（預設 64KB，`task-` 開頭 1MB）。`blob_put` 可帶 `expected_version`
做樂觀鎖，不符回 409 `version_conflict`（附目前 version 與內容）。`task-` 開頭的 key 是
任務層的權威內容，受 `tasks.remote_sync` 開關管制：關閉時 `blob_put` 回 403
`tasks_remote_sync_disabled`，讀取不受影響。`task-authorization:` 開頭（任務層的人類核准
紀錄）不論認證方式一律 403 `authorization_write_forbidden`，唯一寫入路徑是 UI session
限定的 `POST /v1/tasks_authorize`（`api.tasks_admin`）。`task-change:` 開頭的寫入經
`tasks_admin.guard_change_write`：需授權的 change 不可取消標記，要離開 active 或寫入
archive 簿記必須有內容雜湊相符的 UI 核准紀錄（403 `authorization_downgrade_forbidden`／
`authorization_required`），並以守衛讀到的版本做 CAS。任務層停用中（`task-index` 帶
`disabled`）任務內容與 `tasks-snapshot` 的寫入 403 `tasks_disabled`，索引的停用旗標只能
單獨切換（`tasks_admin.guard_task_write`）。

`POST /v1/document_download` 是唯一回二進位的端點：body 同其他 RPC（`space`、
`vault`、`id`，選填 `max_bytes`），成功回原始位元組（Content-Type 為上傳時的 mime、
`Content-Disposition` 帶檔名、`X-Lore-Vault-Sha256`），錯誤仍是 JSON。
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from lore_vault import notes as notes_service
from lore_vault.ask import service as ask_service
from lore_vault.doctor import DoctorContext, default_registry
from lore_vault.doctor.builtin import DEFAULT_BACKLOG_MAX_AGE
from lore_vault.documents import service as document_service
from lore_vault.notes.service import (
    DEFAULT_GET_BUDGET,
    DEFAULT_LIST_BUDGET,
    DEFAULT_LIST_LIMIT,
    FIELDS_FULL,
)
from lore_vault.recall import recall as recall_service
from lore_vault.recall.service import DEFAULT_BUDGET as RECALL_DEFAULT_BUDGET
from lore_vault.recall.service import DEFAULT_LIMIT as RECALL_DEFAULT_LIMIT
from lore_vault.recall.service import MODE_HYBRID
from lore_vault.schema import Vault, canonical_key
from lore_vault.storage import document_index as storage_document_index
from lore_vault.storage import documents as storage_documents
from lore_vault.storage import enrichment as storage_enrichment
from lore_vault.storage import sidecar as storage_sidecar
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.blobs import BlobCorrupt, BlobNotFound
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import ReservedVault, UnknownVault, VaultRequired
from lore_vault.storage.migrate import SCHEMA_VERSION, current_version
from lore_vault.storage.notes import count_notes, list_notes
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import (
    ALL_VAULTS,
    KIND_MISC,
    check_key_prefix,
    get_vault,
    upsert_vault,
    validate_space,
)

from .errors import (
    AuthorizationWriteForbidden,
    DocumentsNotConfigured,
    PayloadTooLarge,
    TasksRemoteSyncDisabled,
    VaultExists,
    error_body,
)
from .principals import principal_of
from .state import AppState
from .tasks_admin import AUTHORIZATION_PREFIX as AUTHORIZATION_KEY_PREFIX
from .tasks_admin import CHANGE_PREFIX as CHANGE_KEY_PREFIX
from .tasks_admin import guard_change_write, guard_task_write

router = APIRouter(prefix="/v1")


def _state(request: Request) -> AppState:
    return request.app.state.lore


# ── 請求模型：未知欄位一律拒絕（打錯參數名不能被默默忽略）──
# vault／space 宣告成可省略：缺值交給服務層拋 VaultRequired／SpaceRequired（400），
# 不讓它變成 422。


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ScopedReq(_Req):
    space: str | None = None


class ResolveRequest(_ScopedReq):
    key: str = Field(description="客戶端以 lore_vault.binding 算出的 binding key")


class CreateVaultRequest(_ScopedReq):
    key: str
    display: str
    kind: str = "repo"
    aliases: list[str] = Field(default_factory=list)


class RecallRequest(_ScopedReq):
    query: str
    vault: str | None = None
    kinds: list[str] | None = None
    limit: int = RECALL_DEFAULT_LIMIT
    budget: int = RECALL_DEFAULT_BUDGET
    mode: str = MODE_HYBRID


class AskRequest(_ScopedReq):
    question: str
    vault: str | None = None
    k: int = ask_service.DEFAULT_K
    # 本輪只支援 note；chunk 列進 unsupported_kinds（文件問答另評估）
    kinds: list[str] | None = None


class GetRequest(_ScopedReq):
    vault: str | None = None
    ids: list[str]
    budget: int = DEFAULT_GET_BUDGET
    # full（預設，含全文）／meta（只回 metadata，不組裝全文、不佔預算）
    fields: str = FIELDS_FULL


class ListRequest(_ScopedReq):
    vault: str | None = None
    since: str | None = None
    # updated 上界（含端點）；與 since 一起做日期區間
    until: str | None = None
    # 頁碼分頁：跳過前面幾筆（與 cursor 擇一）；with_total 另回總筆數
    offset: int | None = Field(default=None, ge=0)
    with_total: bool = False
    topics: list[str] | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIST_LIMIT
    kinds: list[str] | None = None
    # 本頁 note 摘要字數總和上限（title 不計；公平分配，超過配額的截短、
    # 連下限都給不起的尾端 summary 為 null）
    budget: int = DEFAULT_LIST_BUDGET
    # 篩選（UI 的筆記／文件頁）：title 兩種都適用（標題／檔名含該字串）；
    # author／author_state 只屬 note，statuses／extensions 只屬文件
    title: str | None = None
    author: str | None = None
    author_state: str | None = None
    statuses: list[str] | None = None
    extensions: list[str] | None = None


class WriteRequest(_ScopedReq):
    vault: str | None = None
    title: str
    body: str
    # 寫入者自報名（agent 角色名、UI 的 `Xavier (Bernie)`）；未填為 null
    author: str | None = None
    topics: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    # 只跑驗證、範圍、查重與連結解析，不寫入（回 200）
    dry_run: bool = False


class UpdateRequest(_ScopedReq):
    vault: str | None = None
    id: str
    expected_updated: str
    # 這次修改者的自報名，寫進 updated_by（未填為 null，不沿用上一位）
    author: str | None = None
    title: str | None = None
    body: str | None = None
    topics: list[str] | None = None
    links: list[str] | None = None
    # 有傳（含 null）才更新；null 代表清除更正關係
    supersedes: str | None = None


class StatusRequest(_ScopedReq):
    vault: str | None = None


# ── vault ──


def _vault_dict(conn: sqlite3.Connection, vault: Vault) -> dict[str, Any]:
    return {
        "key": vault.key,
        "display": vault.display,
        "kind": vault.kind,
        "space": vault.space,
        "aliases": list(vault.aliases),
        "note_count": count_notes(conn, vault.key, space=vault.space),
    }


def _single_vault(conn: sqlite3.Connection, key: str | None, space: object) -> Vault:
    if key == ALL_VAULTS:
        raise VaultRequired("此操作必須指定單一 vault，不可用 '*'")
    # None → VaultRequired／SpaceRequired
    return get_vault(conn, key, space=space)  # type: ignore[arg-type]


@router.post("/vault_resolve")
def vault_resolve(request: Request, req: ResolveRequest) -> dict[str, Any]:
    """binding key（或別名）→ 該 space 內的現行 vault。服務端讀不到客戶端 cwd，
    key 由客戶端算；key 屬於別的 space 時與不存在相同（404 unknown_vault）。"""
    with _state(request).connection() as conn:
        vault = _single_vault(conn, req.key, req.space)
        result = _vault_dict(conn, vault)
    result["requested"] = req.key
    result["via_alias"] = canonical_key(req.key) != vault.key
    return result


@router.post("/vaults", status_code=status.HTTP_201_CREATED)
def create_vault(request: Request, req: CreateVaultRequest) -> dict[str, Any]:
    """明確建立 vault（只新增：key 或別名已存在回 409，不覆寫既有別名）。

    非 dev space 的 key／別名必須以 `<space>/` 開頭（400 `space_key_prefix_required`）；
    lore／personal 不自動建 global，要時以 `<space>/global` 明確建立。
    """
    space = validate_space(req.space)
    if req.key.strip() == ALL_VAULTS or ALL_VAULTS in req.aliases:
        raise VaultRequired("'*' 保留給跨 vault 查詢，不可當 vault key 或別名")
    if req.kind == KIND_MISC:
        # 雜項 vault 只由 episode 收料自動建立（D14）；key `misc` 另由 upsert_vault 擋
        raise ReservedVault("雜項 vault 只由 episode 收料自動建立，不可明確建立")
    for name in (req.key, *req.aliases):
        if name.strip():
            check_key_prefix(space, name.strip())
    vault = Vault(
        key=req.key,
        display=req.display,
        kind=req.kind,
        aliases=tuple(req.aliases),
        space=space,
    )
    with _state(request).connection() as conn:
        with transaction(conn):
            for candidate in (vault.key, *vault.aliases):
                try:
                    existing = get_vault(conn, candidate, space=space)
                except UnknownVault:
                    continue
                raise VaultExists(
                    f"{candidate!r} 已存在（屬於 vault {existing.key!r}）",
                    _vault_dict(conn, existing),
                )
            upsert_vault(conn, vault)
        return _vault_dict(conn, get_vault(conn, vault.key, space=space))


# ── 七個工具 ──


@router.post("/recall")
def recall(request: Request, req: RecallRequest) -> dict[str, Any]:
    state = _state(request)
    with state.connection() as conn:
        result = recall_service(
            conn,
            req.query,
            req.vault,  # type: ignore[arg-type]
            space=req.space,  # type: ignore[arg-type]  # None → SpaceRequired
            embedder=state.query_embedder,
            dim=state.dim,
            kinds=req.kinds,
            limit=req.limit,
            budget=req.budget,
            mode=req.mode,
        )
    return result.to_dict()


@router.post("/ask")
def ask(request: Request, req: AskRequest) -> dict[str, Any]:
    """recall 的 note 片段交問答模型整理（D11）。檢索完就關連線，再呼叫模型：
    模型呼叫可能要數秒到 `ask.timeout`，不佔著資料庫連線。

    `ask.enabled`（執行期設定，D13）關閉時直接 403 `ask_disabled`，
    不檢索、不呼叫模型。

    查詢 embedding 用 `state.ask_embedder`：一律 `embedding.cold_query_timeout`，
    不像 recall 在模型已載入時用 3 秒短逾時——ask 本來就要等模型數秒，
    embedding 偶發卡住不該讓整個回答降級成只走 lexical。"""
    state = _state(request)
    runtime = state.runtime.current()
    if not runtime.ask.enabled:
        raise ask_service.AskDisabled("問答已由管理者在設定頁關閉")
    with state.connection() as conn:
        context = ask_service.prepare(
            conn,
            req.question,
            req.vault,  # type: ignore[arg-type]
            space=req.space,  # type: ignore[arg-type]  # None → SpaceRequired
            embedder=state.ask_embedder,
            dim=state.dim,
            k=req.k,
            kinds=req.kinds,
            snippet_max_chars=runtime.ask.snippet_max_chars,
        )
    return ask_service.generate(context, state.answerer).to_dict()


@router.post("/get")
def get(request: Request, req: GetRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        result = notes_service.get(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.ids,
            space=req.space,  # type: ignore[arg-type]
            budget=req.budget,
            fields=req.fields,
        )
    return result.to_dict()


@router.post("/list")
def list_(request: Request, req: ListRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        result = notes_service.list_(
            conn,
            req.vault,  # type: ignore[arg-type]
            space=req.space,  # type: ignore[arg-type]
            since=req.since,
            topics=req.topics,
            cursor=req.cursor,
            limit=req.limit,
            kinds=req.kinds,
            budget=req.budget,
            until=req.until,
            offset=req.offset,
            with_total=req.with_total,
            title=req.title,
            author=req.author,
            author_state=req.author_state,
            statuses=req.statuses,
            extensions=req.extensions,
        )
    return result.to_dict()


@router.post("/write", status_code=status.HTTP_201_CREATED)
def write(request: Request, req: WriteRequest, response: Response) -> dict[str, Any]:
    """新增 note（201）。`dry_run: true` 只查重與解析連結、不寫入（200）：
    與正式寫入同一個函式，驗證、vault／space 範圍、supersedes 檢查完全相同。"""
    state = _state(request)
    with state.connection() as conn:
        result = notes_service.write(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.title,
            req.body,
            space=req.space,  # type: ignore[arg-type]
            principal=principal_of(request),
            author=req.author,
            topics=req.topics,
            links=req.links,
            supersedes=req.supersedes,
            embedder=state.query_embedder,
            dim=state.dim,
            dry_run=req.dry_run,
        )
    if req.dry_run:
        response.status_code = status.HTTP_200_OK
    else:
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
            space=req.space,  # type: ignore[arg-type]
            principal=principal_of(request),
            author=req.author,
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

    doctor 是全域對帳（不分 vault／space）；指定 vault 時另附該 vault 的筆數與
    最近更新。無 body 是純健康檢查；有 body 就必須帶 space（400 `space_required`）。
    """
    state = _state(request)
    vault_key = req.vault if req is not None else None
    space = validate_space(req.space) if req is not None else None
    now = datetime.now(UTC)
    # 門檻類設定可由設定頁覆寫（D13）：一律讀執行期有效值
    cfg = state.runtime.current()
    with state.connection() as conn:
        vault_info = None
        if vault_key is not None:
            vault = _single_vault(conn, vault_key, space)
            vault_info = _vault_dict(conn, vault)
            latest, _ = list_notes(conn, vault.key, space=vault.space, limit=1)
            vault_info["last_updated"] = latest[0].updated if latest else None
        report = default_registry().run(
            DoctorContext(
                settings={
                    "embedding_dim": state.dim,
                    "now": now,
                    "enrich_backlog_max_age": DEFAULT_BACKLOG_MAX_AGE,
                    # 未設定備份目錄時 backup.recent 記為 skipped
                    "backup_dir": cfg.backup.dir,
                    "backup_max_age_hours": cfg.backup.max_age_hours,
                    # 未設定 blob_dir 時 blob 對帳記為 skipped
                    "blob_dir": cfg.documents.blob_dir,
                    "documents_stuck_seconds": cfg.documents.stuck_seconds,
                    # 0 = 不警告（tombstones.summary 只當資訊項）
                    "tombstones_warn_age_days": cfg.database.tombstone_warn_age_days,
                    "tombstones_warn_bytes": cfg.database.tombstone_warn_bytes,
                    # ask.provider：問答模型是否可用（只看有沒有建立用戶端，不打網路）；
                    # 管理者關閉問答時記為 skipped
                    "ask_configured": state.answerer is not None,
                    "ask_enabled": cfg.ask.enabled,
                    "ask_model": cfg.ask.model,
                    # episodes.ingest_recency：收料關閉時 skipped（D13）
                    "episodes_ingest": cfg.episodes.ingest,
                    # notes.principal_agreement：設定值與既有 note 是否一致（D12）
                    "principal": state.settings.principal,
                },
                resources={"db": conn},
            )
        )
        backlog = storage_enrichment.enrichment_backlog(
            conn, now=now, max_age_seconds=DEFAULT_BACKLOG_MAX_AGE
        )
        doc_backlog = storage_document_index.backlog(
            conn, now=now, max_age_seconds=DEFAULT_BACKLOG_MAX_AGE
        )
        schema = {"version": current_version(conn), "expected": SCHEMA_VERSION}
    worker = state.worker_status()
    doc_worker = state.documents_worker_status()
    return {
        # 同程序的 worker 起不來（fatal_error）也算不健康
        "ok": report.ok
        and not worker.get("fatal_error")
        and not doc_worker.get("fatal_error"),
        "checked_at": format_utc(now),
        "schema": schema,
        "space": space,
        "vault": vault_info,
        # 暖機失敗不算不健康：只代表剛啟動時 recall／查重可能降級
        # model_loaded：Ollama 目前是否載入模型（/api/ps；無法判斷為 null）。
        # 未載入不是離線：下一次查詢會冷啟動（用 cold_query_timeout），只是較慢
        "embedding": {
            "warmup": state.warmup.status(),
            "model_loaded": state.embedding_model_loaded(),
        },
        "enrich": {
            "worker": worker,
            "backlog": {
                "status": backlog.status,
                "summary": backlog.summary,
                "counts": dict(backlog.counts),
            },
        },
        "documents": {
            "enabled": bool(state.settings.config.documents.blob_dir),
            "worker": doc_worker,
            "backlog": {
                "status": doc_backlog.status,
                "summary": doc_backlog.summary,
                "counts": dict(doc_backlog.counts),
            },
        },
        "doctor": report.to_dict(),
    }


# ── 文件上傳（T-67）──

# multipart 除了檔案本身以外的額外空間（邊界、表頭、vault／space 等欄位）
MULTIPART_OVERHEAD = 64 * 1024
UPLOAD_FIELDS = frozenset({"file", "vault", "space", "filename", "mime"})


def _form_text(form: Any, name: str) -> str | None:
    value = form.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"欄位 {name} 必須是文字")
    return value


@router.post("/documents")
async def upload_document(request: Request) -> JSONResponse:
    """multipart 上傳文件。同 vault 同內容回既有文件（200、`duplicate: true`），
    其餘建列或重排抽取（201）；抽取在背景 worker，回應時 status 多為 pending。"""
    state = _state(request)
    docs = state.settings.config.documents
    if not docs.blob_dir:
        raise DocumentsNotConfigured(
            "服務未設定 documents.blob_dir，不能收文件"
            "（見 config.example.toml 的 [documents]）"
        )
    max_body = docs.max_file_bytes + MULTIPART_OVERHEAD
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > max_body:
        raise PayloadTooLarge(
            f"請求 {length} 位元組，超過上限（檔案 {docs.max_file_bytes} 位元組）"
        )
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ValueError("必須是 multipart/form-data（欄位 file、vault、space）")

    async def limited() -> Any:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body:
                raise PayloadTooLarge(
                    f"上傳內容超過上限（檔案 {docs.max_file_bytes} 位元組）"
                )
            yield chunk

    parser = MultiPartParser(request.headers, limited(), max_files=1, max_fields=8)
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise ValueError(f"multipart 格式錯誤：{exc.message}") from None
    try:
        unknown = sorted(set(form.keys()) - UPLOAD_FIELDS)
        if unknown:
            raise ValueError(f"未知的欄位：{unknown}；可用 {sorted(UPLOAD_FIELDS)}")
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValueError("缺少檔案欄位 file")
        data = await upload.read()
        filename = _form_text(form, "filename") or upload.filename or ""
        mime = _form_text(form, "mime") or upload.content_type
        vault = _form_text(form, "vault")
        space = _form_text(form, "space")
    finally:
        await form.close()

    def run() -> Any:
        with state.connection() as conn:
            return document_service.upload(
                conn,
                state.blob_store(),
                vault,  # type: ignore[arg-type]  # None → VaultRequired
                data,
                space=space,  # type: ignore[arg-type]  # None → SpaceRequired
                filename=filename,
                mime=mime,
                max_bytes=docs.max_file_bytes,
            )

    result = await run_in_threadpool(run)
    if not result.duplicate:
        state.wake_documents()
    return JSONResponse(
        result.to_dict(),
        status_code=status.HTTP_200_OK if result.duplicate else status.HTTP_201_CREATED,
    )


# ── 原始檔下載 ──

# 回應 header：原始檔的 sha256（讀出時已驗過；客戶端寫檔前再比一次）
HEADER_DOCUMENT_SHA256 = "X-Lore-Vault-Sha256"
HEADER_DOCUMENT_ID = "X-Lore-Vault-Document-Id"


class DocumentDownloadRequest(_ScopedReq):
    vault: str | None = None
    id: str
    # 呼叫端能收的上限（位元組）；文件超過時回 413 `too_large`，不讀 blob
    max_bytes: int | None = Field(default=None, gt=0)


def content_disposition(filename: str) -> str:
    """`attachment`；ASCII 後備檔名去掉引號與控制字元，完整名稱以 RFC 5987 帶。"""
    fallback = (
        "".join(
            ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename
        ).strip()
        or "download"
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.post("/document_download")
def document_download(request: Request, req: DocumentDownloadRequest) -> Response:
    """取回文件原始檔（上傳時的位元組）。範圍同 get：vault／space 內找不到一律 404；
    讀 blob 時驗 sha256，遺失或損毀回 500（`blob_missing`／`blob_corrupt`），
    不回傳可能損毀的內容。墓碑中的文件不可下載（先 undelete）。"""
    state = _state(request)
    if not state.settings.config.documents.blob_dir:
        raise DocumentsNotConfigured("服務未設定 documents.blob_dir，不能下載文件")
    with state.connection() as conn:
        doc = storage_documents.get_document(
            conn,
            req.vault,  # type: ignore[arg-type]  # None → VaultRequired
            req.id,
            space=req.space,  # type: ignore[arg-type]  # None → SpaceRequired
        )
    if req.max_bytes is not None and doc.size_bytes > req.max_bytes:
        raise PayloadTooLarge(
            f"文件 {doc.id!r} 為 {doc.size_bytes} 位元組，超過這次請求的上限 "
            f"{req.max_bytes} 位元組"
        )
    try:
        data = state.blob_store().read(doc.sha256)
    except BlobNotFound as exc:
        return JSONResponse(error_body("blob_missing", str(exc)), status_code=500)
    except BlobCorrupt as exc:
        return JSONResponse(error_body("blob_corrupt", str(exc)), status_code=500)
    return Response(
        content=data,
        media_type=doc.mime or "application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(doc.filename),
            HEADER_DOCUMENT_SHA256: doc.sha256,
            HEADER_DOCUMENT_ID: doc.id,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── 側載小型機器狀態（schema v17，v18 加版本）──


class BlobPutRequest(_ScopedReq):
    vault: str | None = None
    key: str
    mime: str | None = None
    content_base64: str
    # 樂觀鎖：省略＝不比對（整份覆寫）；0＝預期尚不存在；正整數＝須等於目前版本
    expected_version: int | None = Field(None, ge=0)


class BlobGetRequest(_ScopedReq):
    # 省略（或 `*`）＝本 space 內所有存過該 key 的 vault
    vault: str | None = None
    key: str


def _decode_sidecar(text: str, key: str) -> bytes:
    limit = storage_sidecar.limit_for_key(key)
    # base64 長度上限：解碼後 limit 的 base64 長度（含 padding），先擋免得解碼大字串
    if len(text) > 4 * ((limit + 2) // 3):
        raise storage_sidecar.SidecarTooLarge(f"內容超過上限 {limit} 位元組")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content_base64 不是合法的 base64") from None


@router.post("/blob_put")
def blob_put(request: Request, req: BlobPutRequest) -> dict[str, Any]:
    """覆寫 `(vault, key)` 的內容（不留舊內容），版本遞增；回 `{updated, version}`。

    vault 必填、可用別名；key 含路徑分隔等字元 400 `invalid_key`；解碼後超過該 key
    的上限 413 `too_large`；`expected_version` 不符 409 `version_conflict`
    （`error.current` 為目前內容，key 不存在時為 null）；`task-authorization:`
    開頭一律 403 `authorization_write_forbidden`（只能經 `/v1/tasks_authorize`）；
    `task-` 開頭且 `tasks.remote_sync` 關閉 403 `tasks_remote_sync_disabled`
    （不寫任何東西）；任務層停用中，任務內容與 `tasks-snapshot` 403 `tasks_disabled`
    （`guard_task_write`）；`task-change:` 開頭再過授權守衛（`guard_change_write`）。"""
    storage_sidecar.validate_key(req.key)
    if req.key.startswith(AUTHORIZATION_KEY_PREFIX):
        # 不論 bearer 或 UI session：授權紀錄只能經 /v1/tasks_authorize 寫入
        raise AuthorizationWriteForbidden(
            "task-authorization: 開頭的授權紀錄不可直接寫入；"
            "請在 UI 任務頁按「核准」（POST /v1/tasks_authorize）"
        )
    if (
        storage_sidecar.is_task_key(req.key)
        and not _state(request).runtime.current().tasks.remote_sync
    ):
        raise TasksRemoteSyncDisabled(
            "服務未開啟任務層遠端同步（設定 tasks.remote_sync）；"
            "task- 開頭的側載不收，任務層請在本機操作"
        )
    content = _decode_sidecar(req.content_base64, req.key)
    with _state(request).connection() as conn:
        # 任務層停用守衛：停用中拒收任務內容；索引的停用旗標只能單獨切換
        expected = guard_task_write(
            conn,
            req.vault,
            req.key,
            content,
            space=req.space,
            expected_version=req.expected_version,
        )
        if req.key.startswith(CHANGE_KEY_PREFIX):
            # 不論認證方式：需授權 change 的閘門在服務端強制（bearer 由所有 agent 共用）
            expected = guard_change_write(
                conn,
                req.vault,
                req.key,
                content,
                space=req.space,
                expected_version=req.expected_version,
            )
        blob = storage_sidecar.put(
            conn,
            req.vault,
            req.key,
            content,
            space=req.space,
            mime=req.mime,
            expected_version=expected,
        )
    return {"updated": blob.updated, "version": blob.version}


@router.post("/blob_get")
def blob_get(request: Request, req: BlobGetRequest) -> dict[str, Any]:
    """給了 vault：回該份內容，沒有 404 `not_found`。省略 vault：回
    `{"items": [...]}`（本 space 內所有存過該 key 的 vault，可能為空）。"""
    with _state(request).connection() as conn:
        if storage_sidecar.is_all(req.vault):
            items = storage_sidecar.list_for_key(conn, req.key, space=req.space)
            return {"items": [b.to_dict() for b in items]}
        blob = storage_sidecar.get(conn, req.vault, req.key, space=req.space)
    return blob.to_dict()


# ── 唯讀快照（T-31）──


@router.get("/snapshot")
def snapshot(request: Request) -> Response:
    """MCP 殼降級用的一致性唯讀副本：vaults、別名、notes、FTS（不含向量與 episode）。

    - 服務端快取最近一份；資料指紋（白名單資料表的內容雜湊）未變就不重建
    - ETag 為快照檔 sha256；`If-None-Match` 符合回 304（不傳檔）
    - 版本、產生時間、sha256、筆數放在 header（304 也帶，殼端據此更新確認時間）
    """
    state = _state(request)
    info, content = state.snapshot_cache().get(request.headers.get("if-none-match"))
    headers = {
        "ETag": storage_snapshot.etag(info.sha256),
        storage_snapshot.HEADER_GENERATED_AT: info.generated_at,
        storage_snapshot.HEADER_SCHEMA_VERSION: str(info.schema_version),
        storage_snapshot.HEADER_SERVICE_VERSION: request.app.version,
        storage_snapshot.HEADER_SHA256: info.sha256,
        storage_snapshot.HEADER_NOTES: str(info.notes),
        "Cache-Control": "no-cache",
    }
    if content is None:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=content, media_type=storage_snapshot.MEDIA_TYPE, headers=headers
    )
