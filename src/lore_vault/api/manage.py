"""UI 管理端點（T-70～T-75）：vault 列表／編輯／別名、換 space、刪除與墓碑、
文件復原與重試、concept 瀏覽、episode 統計。沿用 `/v1` 的 RPC 慣例（一律 POST、
body 必帶 `space`、未知欄位 422）與同一套認證、錯誤碼對照。

破壞性操作（`vault_delete`、`note_delete`、`document_delete`、`vault_move_space`）
兩段式確認：

1. 不帶 `confirm_token` → 只規劃（唯讀），回 `{executed: false, plan, confirm_token,
   expires_at}`
2. 帶上 `confirm_token` 且其餘參數與第 1 步**完全相同** → 驗證後執行，回
   `{executed: true, plan, ...}`

token = base64url(JSON payload) + "." + base64url(HMAC-SHA256)。payload 綁定操作名、
請求參數（含 space 與 reason）、規劃內容的 sha256（`digest`）與到期時間；祕密為每個
app 程序隨機產生（服務重啟後舊 token 失效）。驗證失敗 400 `invalid_confirm_token`、
過期 400 `confirm_token_expired`。執行時在同一個寫入交易（BEGIN IMMEDIATE）內重新
規劃、比對 digest，不符 409 `plan_changed`（附目前的規劃，需重新確認），相符才執行，
沒有 check-then-act 的窗口；儲存層自身的筆數核對（`PlanChanged`）是第二道防線。
執行後目標已不存在，同一個 token 重送會 404，不會重複執行。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from lore_vault.notes import InvalidCursor
from lore_vault.schema import canonical_key
from lore_vault.storage import admin
from lore_vault.storage import manage as store
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import UnknownVault
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import validate_space

from .errors import (
    ConfirmPlanChanged,
    ConfirmTokenExpired,
    ConfirmTokenInvalid,
    DocumentsNotConfigured,
)
from .state import AppState

router = APIRouter(prefix="/v1")

CONFIRM_TTL_SECONDS = 300
DEFAULT_PAGE = 50


def _state(request: Request) -> AppState:
    return request.app.state.lore


# ── 確認 token ──


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def plan_digest(fingerprint: Any) -> str:
    return hashlib.sha256(_canonical(fingerprint)).hexdigest()


@dataclass
class ConfirmSigner:
    """簽發與驗證確認 token。祕密與時鐘掛在 app.state 上（測試可注入時鐘）。"""

    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    ttl: float = CONFIRM_TTL_SECONDS
    clock: Callable[[], float] = time.time

    def _sign(self, body: bytes) -> str:
        return _b64(hmac.new(self.secret, body, hashlib.sha256).digest())

    def issue(self, op: str, args: dict[str, Any], digest: str) -> tuple[str, float]:
        expires = self.clock() + self.ttl
        payload = {"op": op, "args": args, "digest": digest, "exp": expires}
        body = _canonical(payload)
        return f"{_b64(body)}.{self._sign(body)}", expires

    def verify(self, token: str, op: str, args: dict[str, Any]) -> str:
        """回傳 token 綁定的規劃 digest。簽章在前：竄改過的內容一律不解讀。"""
        try:
            encoded, signature = token.split(".")
            body = _unb64(encoded)
        except (ValueError, binascii.Error):
            raise ConfirmTokenInvalid("確認 token 格式錯誤") from None
        if not hmac.compare_digest(signature, self._sign(body)):
            raise ConfirmTokenInvalid("確認 token 簽章不符（遭竄改或來自其他服務程序）")
        payload = json.loads(body)
        if payload.get("op") != op or payload.get("args") != args:
            raise ConfirmTokenInvalid(
                "確認 token 與這次請求的操作或參數不符；請以規劃時相同的參數送出"
            )
        if self.clock() > float(payload["exp"]):
            raise ConfirmTokenExpired("確認 token 已過期，請重新規劃")
        return str(payload["digest"])


_SIGNER_LOCK = threading.Lock()


def _signer(request: Request) -> ConfirmSigner:
    app_state = request.app.state
    with _SIGNER_LOCK:
        signer = getattr(app_state, "lore_confirm", None)
        if signer is None:
            signer = ConfirmSigner()
            app_state.lore_confirm = signer
    return signer


Planner = Callable[[sqlite3.Connection], tuple[dict[str, Any], Any]]
Executor = Callable[[sqlite3.Connection], dict[str, Any]]


def _two_phase(
    request: Request,
    op: str,
    args: dict[str, Any],
    token: str | None,
    plan: Planner,
    execute: Executor,
) -> dict[str, Any]:
    """`plan(conn) -> (回給客戶端的規劃, 指紋)`；`execute(conn) -> 額外回應欄位`。"""
    signer = _signer(request)
    with _state(request).connection() as conn:
        if token is None:
            shown, fingerprint = plan(conn)
            issued, expires = signer.issue(op, args, plan_digest(fingerprint))
            return {
                "executed": False,
                "plan": shown,
                "confirm_token": issued,
                "expires_at": format_utc(datetime.fromtimestamp(expires, UTC)),
            }
        expected = signer.verify(token, op, args)
        with transaction(conn):
            shown, fingerprint = plan(conn)
            if plan_digest(fingerprint) != expected:
                raise ConfirmPlanChanged(
                    "規劃後資料已變動，請確認新的規劃後重新送出", shown
                )
            extra = execute(conn)
    return {"executed": True, "plan": shown, **extra}


def _formal_key_in_space(conn: sqlite3.Connection, key: str, space: object) -> str:
    """刪 vault／換 space 只接受正式 key，且必須屬於 `space`；否則一律
    `UnknownVault`，訊息不帶任何其他 vault 的資訊（別名提示可能指向別的 space）。"""
    checked = validate_space(space)
    canonical = canonical_key(key)
    row = conn.execute(
        "SELECT space FROM vaults WHERE key = ?", (canonical,)
    ).fetchone()
    if row is None or row[0] != checked:
        raise UnknownVault(
            f"space {checked!r} 內沒有正式 key 為 {canonical!r} 的 vault"
            "（此操作不接受別名）"
        )
    return canonical


def _latest(conn: sqlite3.Connection, table: str, key: str) -> str | None:
    return conn.execute(
        f"SELECT max(updated) FROM {table} WHERE vault = ?", (key,)
    ).fetchone()[0]


# ── 請求模型 ──


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ScopedReq(_Req):
    # 缺值交給服務層拋 SpaceRequired（400），不讓它變成 422
    space: str | None = None


class VaultListRequest(_ScopedReq):
    pass


class VaultUpdateRequest(_ScopedReq):
    vault: str | None = None
    display: str


class AliasRequest(_ScopedReq):
    vault: str | None = None
    alias: str


class MoveSpaceRequest(_ScopedReq):
    key: str
    to_space: str
    new_key: str | None = None
    confirm_token: str | None = None


class VaultDeleteRequest(_ScopedReq):
    key: str
    reason: str | None = None
    confirm_token: str | None = None


class ItemDeleteRequest(_ScopedReq):
    vault: str | None = None
    id: str
    reason: str | None = None
    confirm_token: str | None = None


class TombstonesRequest(_ScopedReq):
    vault: str | None = None
    kinds: list[str] | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE


class UndeleteRequest(_ScopedReq):
    id: str


class DocumentRetryRequest(_ScopedReq):
    vault: str | None = None
    id: str


class ConceptQueryRequest(_ScopedReq):
    vault: str | None = None
    scope: str | None = None
    scope_state: str | None = None
    kind: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE


class EpisodeSummaryRequest(_ScopedReq):
    vault: str | None = None


def _args(req: BaseModel) -> dict[str, Any]:
    """token 綁定的請求參數：全部欄位（含 None），不含 token 本身。"""
    return req.model_dump(exclude={"confirm_token"})


def _encode_cursor(parts: tuple[str, ...] | None) -> str | None:
    if parts is None:
        return None
    return _b64(_canonical(list(parts)))


def _decode_cursor(cursor: str | None, size: int) -> tuple[str, ...] | None:
    if cursor is None:
        return None
    try:
        data = json.loads(_unb64(cursor))
    except (ValueError, UnicodeError, binascii.Error):
        raise InvalidCursor(f"cursor 無法解析：{cursor!r}") from None
    if (
        not isinstance(data, list)
        or len(data) != size
        or not all(isinstance(x, str) for x in data)
    ):
        raise InvalidCursor(f"cursor 格式錯誤：{cursor!r}")
    return tuple(data)


# ── vault 列表與編輯（T-70）、別名（T-71）──


@router.post("/vault_list")
def vault_list(request: Request, req: VaultListRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        items = store.list_vault_summaries(conn, space=req.space)
    return {"space": req.space, "vaults": [v.to_dict() for v in items]}


@router.post("/vault_update")
def vault_update(request: Request, req: VaultUpdateRequest) -> dict[str, Any]:
    """只改顯示名稱。"""
    with _state(request).connection() as conn:
        return store.update_display(
            conn, req.vault, req.display, space=req.space
        ).to_dict()


@router.post("/vault_alias_add")
def vault_alias_add(request: Request, req: AliasRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        return store.add_alias(conn, req.vault, req.alias, space=req.space).to_dict()


@router.post("/vault_alias_remove")
def vault_alias_remove(request: Request, req: AliasRequest) -> dict[str, Any]:
    with _state(request).connection() as conn:
        return store.remove_alias(conn, req.vault, req.alias, space=req.space).to_dict()


# ── 換 space（T-72，A20）──


@router.post("/vault_move_space")
def vault_move_space(request: Request, req: MoveSpaceRequest) -> dict[str, Any]:
    """只允許 lore↔personal；key 與別名換成新前綴，舊 key 不留別名。"""

    def plan(conn: sqlite3.Connection) -> tuple[dict[str, Any], Any]:
        key = _formal_key_in_space(conn, req.key, req.space)
        result = admin.plan_space_change(conn, key, req.to_space, new_key=req.new_key)
        shown = result.to_dict()
        return shown, shown

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        key = canonical_key(req.key)
        result = admin.change_vault_space(conn, key, req.to_space, new_key=req.new_key)
        return {
            "vault": store.vault_summary(
                conn, result.new_key, space=result.to_space
            ).to_dict()
        }

    return _two_phase(
        request, "vault_move_space", _args(req), req.confirm_token, plan, execute
    )


# ── 刪除（T-73）──


def _reason(value: str | None, default: str) -> str:
    return default if value is None else value


@router.post("/vault_delete")
def vault_delete(request: Request, req: VaultDeleteRequest) -> dict[str, Any]:
    """刪整個 vault（只接受正式 key）。確認即等同 CLI 的 `--force`：規劃內容
    （含 `requires_force` 與各表筆數）已在第一步列出並綁進 token。"""
    reason = _reason(req.reason, admin.DEFAULT_VAULT_REASON)

    def plan(conn: sqlite3.Connection) -> tuple[dict[str, Any], Any]:
        key = _formal_key_in_space(conn, req.key, req.space)
        shown = admin.plan_vault_deletion(conn, key).to_dict()
        fingerprint = {
            "plan": shown,
            "notes_updated": _latest(conn, "notes", key),
            "documents_updated": _latest(conn, "documents", key),
        }
        return shown, fingerprint

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        admin.delete_vault(conn, canonical_key(req.key), force=True, reason=reason)
        return {}

    return _two_phase(
        request, "vault_delete", _args(req), req.confirm_token, plan, execute
    )


@router.post("/note_delete")
def note_delete(request: Request, req: ItemDeleteRequest) -> dict[str, Any]:
    reason = _reason(req.reason, admin.DEFAULT_NOTE_REASON)

    def plan(conn: sqlite3.Connection) -> tuple[dict[str, Any], Any]:
        result = admin.plan_note_deletion(
            conn,
            req.vault,  # type: ignore[arg-type]  # None → VaultRequired
            req.id,
            space=req.space,  # type: ignore[arg-type]  # None → SpaceRequired
        )
        shown = result.to_dict()
        updated = conn.execute(
            "SELECT updated FROM notes WHERE id = ?", (req.id,)
        ).fetchone()[0]
        return shown, {"plan": shown, "updated": updated}

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        admin.delete_note(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
            reason=reason,
        )
        return {}

    return _two_phase(
        request, "note_delete", _args(req), req.confirm_token, plan, execute
    )


@router.post("/document_delete")
def document_delete(request: Request, req: ItemDeleteRequest) -> dict[str, Any]:
    """刪單份文件；blob 不刪（沒人引用時 doctor 回報、`gc-blobs` 清理）。"""
    state = _state(request)
    reason = _reason(req.reason, admin.DEFAULT_DOCUMENT_REASON)

    def plan(conn: sqlite3.Connection) -> tuple[dict[str, Any], Any]:
        result = admin.plan_document_deletion(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
        )
        shown = result.to_dict()
        row = conn.execute(
            "SELECT status, updated, supersedes FROM documents WHERE id = ?",
            (req.id,),
        ).fetchone()
        return shown, {"plan": shown, "row": list(row)}

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        admin.delete_document(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
            reason=reason,
        )
        return {}

    result = _two_phase(
        request, "document_delete", _args(req), req.confirm_token, plan, execute
    )
    if result["executed"]:
        # 刪掉現行版本時前一版回到索引，向量由 worker 補
        state.wake_documents()
    return result


# ── 墓碑與復原（T-73、T-74）──


@router.post("/tombstones")
def tombstones(request: Request, req: TombstonesRequest) -> dict[str, Any]:
    """note 與文件墓碑（只有 metadata），依刪除時間由新到舊分頁。vault 必填，
    `"*"` 為 space 內全部；已刪除的 vault 可用原 key 查。"""
    cursor = _decode_cursor(req.cursor, 3)
    with _state(request).connection() as conn:
        items, next_cursor = store.list_tombstones(
            conn,
            req.vault,
            space=req.space,
            kinds=req.kinds,
            limit=req.limit,
            cursor=cursor,  # type: ignore[arg-type]
        )
    return {"items": items, "next_cursor": _encode_cursor(next_cursor)}


@router.post("/note_undelete")
def note_undelete(request: Request, req: UndeleteRequest) -> dict[str, Any]:
    """沿用 CLI `undelete-note`：只移除墓碑，note 內容**不會**回來；有匯入來源的
    note 在下次重跑匯入時匯回（`reimportable`），其餘移除墓碑後即無從復原。"""
    with _state(request).connection() as conn:
        with transaction(conn):
            store.note_tombstone_in_space(conn, req.id, space=req.space)
            grave = admin.undelete_note(conn, req.id)
    return {
        "undeleted": grave,
        "restored": False,
        "reimportable": grave["source"] is not None,
    }


@router.post("/document_undelete")
def document_undelete(request: Request, req: UndeleteRequest) -> dict[str, Any]:
    """以墓碑 metadata 與仍在的原始檔重建文件（同一 id），重新排入抽取。"""
    state = _state(request)
    if not state.settings.config.documents.blob_dir:
        raise DocumentsNotConfigured("服務未設定 documents.blob_dir，不能復原文件")
    blobs = state.blob_store()
    with state.connection() as conn:
        with transaction(conn):
            store.document_tombstone_in_space(conn, req.id, space=req.space)
            result = admin.undelete_document(
                conn,
                req.id,
                space=req.space,  # type: ignore[arg-type]
                blob_ok=lambda sha: blobs.verify(sha) == "ok",
            )
    state.wake_documents()
    return {
        "document": result["document"].to_dict(),
        "space": req.space,
        "tombstone": result["tombstone"],
    }


@router.post("/document_retry")
def document_retry(request: Request, req: DocumentRetryRequest) -> dict[str, Any]:
    """failed 文件重排抽取（人工次數上限 `storage.manage.MAX_MANUAL_RETRIES`）。"""
    state = _state(request)
    with state.connection() as conn:
        doc, used = store.retry_document(conn, req.vault, req.id, space=req.space)
    state.wake_documents()
    return {
        "document": doc.to_dict(),
        "space": req.space,
        "manual_retries": used,
        "max_manual_retries": store.MAX_MANUAL_RETRIES,
    }


# ── concept 瀏覽與 episode 統計（T-75）──


@router.post("/concept_query")
def concept_query(request: Request, req: ConceptQueryRequest) -> dict[str, Any]:
    """concept metadata 與 statement；不含 probe／why／evidence 等引用對話欄位。"""
    cursor = _decode_cursor(req.cursor, 2)
    with _state(request).connection() as conn:
        items, next_cursor = store.query_concepts(
            conn,
            req.vault,
            space=req.space,
            scope=req.scope,
            scope_state=req.scope_state,
            kind=req.kind,
            limit=req.limit,
            cursor=cursor,  # type: ignore[arg-type]
        )
    return {"items": items, "next_cursor": _encode_cursor(next_cursor)}


@router.post("/episode_summary")
def episode_summary(request: Request, req: EpisodeSummaryRequest) -> dict[str, Any]:
    """各 machine／vault 的 episode 筆數與最近時間，不含任何對話原文。"""
    with _state(request).connection() as conn:
        summary = store.episode_summary(conn, req.vault, space=req.space)
    return {"space": req.space, "vault": req.vault, **summary}
