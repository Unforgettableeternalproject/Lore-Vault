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
規劃、比對 digest，不符 409 `plan_changed`（附目前的規劃，以及綁定新規劃的
`confirm_token`／`expires_at`；仍需使用者再確認一次、以新 token 重送，服務端不會自動
執行），相符才執行，沒有 check-then-act 的窗口；儲存層自身的筆數核對（`PlanChanged`）
是第二道防線（不附 plan／token）。舊 token 綁的是舊規劃的 digest：只要資料維持在新狀態，
重送舊 token 一律 409、不執行（token 無狀態，資料若恢復成舊規劃的樣子，
舊 token 才又相符）。
執行後目標已不存在，同一個 token 重送會 404，不會重複執行。

另有唯讀的 `topics`（標籤與使用筆數，UI 篩選用；不提供 MCP 工具）。
"""

from __future__ import annotations

import binascii
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from lore_vault.confirm import (
    ConfirmSigner,
    plan_digest,
)
from lore_vault.confirm import b64 as _b64
from lore_vault.confirm import canonical as _canonical
from lore_vault.confirm import unb64 as _unb64
from lore_vault.notes import InvalidCursor
from lore_vault.notes import review as review_service
from lore_vault.proposals import policy
from lore_vault.proposals import types as proposal_types
from lore_vault.schema import canonical_key
from lore_vault.storage import admin
from lore_vault.storage import manage as store
from lore_vault.storage import proposals as storage_proposals
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import UnknownVault
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import resolve_write, validate_space

from .errors import ConfirmPlanChanged, DocumentsNotConfigured, UiSessionRequired
from .principals import actor_of, principal_of
from .state import AppState

router = APIRouter(prefix="/v1")

DEFAULT_PAGE = 50


def _state(request: Request) -> AppState:
    return request.app.state.lore


# ── 確認 token（簽章邏輯在 `lore_vault.confirm`，殼共用）──


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


@dataclass(frozen=True)
class DeleteTarget:
    """刪除操作要綁定的提案目標（note_delete／document_delete）。

    `summary(conn, vault_key)` 回提案的 `plan_summary`（給審核頁看的穩定內容，
    不放 embedding／補算這類背景會變的計數，否則去重會一直失效）。"""

    kind: str
    vault: str | None
    space: str | None
    target_id: str
    reason: str
    summary: Callable[[sqlite3.Connection, str], dict[str, Any]]


def _proposal_view(proposal: Any, *, deduped: bool = False) -> dict[str, Any]:
    return {**proposal.brief(), "deduped": deduped}


def _two_phase(
    request: Request,
    op: str,
    args: dict[str, Any],
    token: str | None,
    plan: Planner,
    execute: Executor,
    *,
    target: DeleteTarget | None = None,
) -> dict[str, Any]:
    """`plan(conn) -> (回給客戶端的規劃, 指紋)`；`execute(conn) -> 額外回應欄位`。

    `target`（刪除 note／文件）：一般 agent（bearer 非 uep、dev／lore）的刪除同時綁定
    一筆開放提案——第一步在寫入交易內 upsert 提案，指紋納入 {提案 id, rev}；
    第二步在同一交易內把提案轉 applied(via=token) 再刪除，轉移失敗整段 rollback
    （`proposal_resolved`）。UI 核准提案後舊 token 的指紋對不上，不會再執行。
    """
    signer = _signer(request)
    # token 同時綁定簽發者的 principal：別的憑證不能拿去執行
    args = {**args, "principal": principal_of(request)}
    actor = actor_of(request)
    governed = target is not None and policy.decide_delete(actor, target.space or "")
    degraded: list[bool] = []

    def bind(conn: sqlite3.Connection, fingerprint: Any, *, upsert: bool):
        """回 (指紋, 提案或 None, deduped)。upsert=False 只查既有的開放提案。"""
        if target is None:
            return fingerprint, None, False
        vault_key = resolve_write(conn, target.vault, space=target.space)
        if upsert and governed:
            try:
                up = storage_proposals.upsert_open(
                    conn,
                    space=target.space,  # type: ignore[arg-type]
                    vault=vault_key,
                    kind=target.kind,
                    target_id=target.target_id,
                    payload={
                        "reason": target.reason,
                        "plan_summary": target.summary(conn, vault_key),
                    },
                    proposer_principal=actor.principal,
                )
            except proposal_types.ProposalQueueFull:
                # 降級：不建提案、指紋標記為無提案，對話授權（confirm_token）仍可用
                degraded.append(True)
                return {"fp": fingerprint, "proposal": None}, None, False
            prop, deduped = up.proposal, up.deduped
        else:
            storage_proposals.expire_due(conn)
            prop, deduped = (
                storage_proposals.find_open(
                    conn, vault_key, target.kind, target.target_id
                ),
                False,
            )
        marker = None if prop is None else [prop.id, prop.rev]
        return {"fp": fingerprint, "proposal": marker}, prop, deduped

    with _state(request).connection() as conn:
        if token is None:
            with transaction(conn) if governed else nullcontext():
                shown, fingerprint = plan(conn)
                fingerprint, prop, deduped = bind(conn, fingerprint, upsert=True)
            issued, expires = signer.issue(op, args, plan_digest(fingerprint))
            response = {
                "executed": False,
                "plan": shown,
                "confirm_token": issued,
                "expires_at": format_utc(datetime.fromtimestamp(expires, UTC)),
            }
            if governed and prop is not None:
                response["proposal"] = _proposal_view(prop, deduped=deduped)
                response["proposal_pending"] = True
            elif degraded:
                response["proposal_pending"] = False
                response["proposal_note"] = (
                    "待審提案已達上限，這次沒有登記提案；仍可在對話中取得使用者"
                    "明確同意後帶 confirm_token 執行，或請使用者先處理審核頁的積壓"
                )
            return response
        expected = signer.verify(token, op, args)
        changed: tuple[dict[str, Any], Any] | None = None
        with transaction(conn):
            shown, raw = plan(conn)
            fingerprint, prop, _ = bind(conn, raw, upsert=False)
            if plan_digest(fingerprint) != expected:
                # 重新規劃並（受管的刪除）重新 upsert 提案，讓新 token 綁在開放提案上；
                # 離開交易（提交）後才拋，否則 upsert 會跟著 rollback
                fingerprint, prop, _ = bind(conn, raw, upsert=True)
                changed = (shown, fingerprint)
            else:
                if (
                    governed
                    and prop is not None
                    and not storage_proposals.transition(
                        conn,
                        prop.id,
                        expect_state=proposal_types.PENDING,
                        expect_rev=prop.rev,
                        to_state=proposal_types.APPLIED,
                        via=proposal_types.VIA_TOKEN,
                        by=actor.principal,
                    )
                ):
                    raise proposal_types.ProposalResolved(
                        f"提案 {prop.id} 已被另一個通道處理"
                    )
                if (
                    not governed
                    and target is not None
                    and actor.kind == proposal_types.ACTOR_UI
                ):
                    # UI 直接刪除：該目標的開放提案隨之過時
                    storage_proposals.close_for_target(
                        conn,
                        resolve_write(conn, target.vault, space=target.space),
                        target.target_id,
                        to_state=proposal_types.SUPERSEDED,
                        via=proposal_types.VIA_UI_DIRECT,
                        reason="target deleted by user",
                    )
                extra = execute(conn)
        if changed is not None:
            shown, fingerprint = changed
            # 綁定新規劃的 token：仍要使用者看過新規劃再送一次，不在這裡執行
            issued, expires = signer.issue(op, args, plan_digest(fingerprint))
            raise ConfirmPlanChanged(
                "規劃後資料已變動，請確認新的規劃後以新的 confirm_token 重新送出",
                shown,
                confirm_token=issued,
                expires_at=format_utc(datetime.fromtimestamp(expires, UTC)),
            )
    result: dict[str, Any] = {"executed": True, "plan": shown, **extra}
    if governed and prop is not None:
        result["proposal"] = {
            "id": prop.id,
            "kind": prop.kind,
            "state": proposal_types.APPLIED,
            "resolved_via": proposal_types.VIA_TOKEN,
        }
    return result


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
    # updated 區間（含端點）與頁碼分頁（offset 與 cursor 擇一；with_total 另回總筆數）
    since: str | None = None
    until: str | None = None
    offset: int = Field(default=0, ge=0)
    with_total: bool = False


class ConceptGraphRequest(_ScopedReq):
    vault: str | None = None
    scope: str
    scope_state: str | None = None
    kind: str | None = None
    since: str | None = None
    until: str | None = None
    # 範圍檢查（max_nodes 上限、fanout 下限）在儲存層，違規回 400 invalid_request
    max_nodes: int = store.GRAPH_DEFAULT_MAX_NODES
    anchor_fanout_cap: int = store.GRAPH_DEFAULT_FANOUT_CAP


class ConceptScopesRequest(_ScopedReq):
    vault: str | None = None
    scope_state: str | None = None
    kind: str | None = None


class EpisodeSummaryRequest(_ScopedReq):
    vault: str | None = None


class TopicsRequest(_ScopedReq):
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


def _vault_level_ui_only(request: Request, space: str | None) -> None:
    """vault 層級的破壞性操作（整個 vault 刪除、換 space）會連同人類寫的筆記一起
    帶走，不經逐筆提案：dev／lore 的一般 agent（bearer）一律只能由 UI 操作。"""
    if policy.decide_delete(actor_of(request), space or ""):
        raise UiSessionRequired(
            "整個 vault 的刪除與換 space 只能由登入 UI 的使用者本人操作；"
            "請使用者在 UI 操作"
        )


@router.post("/vault_move_space")
def vault_move_space(request: Request, req: MoveSpaceRequest) -> dict[str, Any]:
    """只允許 lore↔personal；key 與別名換成新前綴，舊 key 不留別名。"""
    _vault_level_ui_only(request, req.space)

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


def store_note(conn: sqlite3.Connection, req: Any) -> Any:
    """範圍內取 note（vault／space 檢查同其他讀寫）。"""
    from lore_vault.storage.notes import get_note

    return get_note(conn, req.vault, req.id, space=req.space)


def _reason(value: str | None, default: str) -> str:
    return default if value is None else value


@router.post("/vault_delete")
def vault_delete(request: Request, req: VaultDeleteRequest) -> dict[str, Any]:
    """刪整個 vault（只接受正式 key）。確認即等同 CLI 的 `--force`：規劃內容
    （含 `requires_force` 與各表筆數）已在第一步列出並綁進 token。"""
    _vault_level_ui_only(request, req.space)
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


def _note_summary(conn: sqlite3.Connection, vault_key: str, note_id: str) -> dict:
    """提案的 plan_summary（note）：標題與連結數，內容穩定、不含背景計數。"""
    row = conn.execute(
        "SELECT title, links FROM notes WHERE id = ? AND vault = ?",
        (note_id, vault_key),
    ).fetchone()
    backlinks = conn.execute(
        "SELECT count(*) FROM notes WHERE vault = ? AND id != ? AND EXISTS "
        "(SELECT 1 FROM json_each(notes.links) WHERE value = ?)",
        (vault_key, note_id, note_id),
    ).fetchone()[0]
    return {
        "title": row["title"],
        "link_count": len(json.loads(row["links"])),
        "backlink_count": int(backlinks),
    }


def _document_summary(
    conn: sqlite3.Connection, vault_key: str, document_id: str
) -> dict:
    row = conn.execute(
        "SELECT filename, mime, size_bytes, version FROM documents "
        "WHERE id = ? AND vault = ?",
        (document_id, vault_key),
    ).fetchone()
    return {
        "title": row["filename"],
        "filename": row["filename"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "version": row["version"],
    }


@router.post("/note_delete")
def note_delete(request: Request, req: ItemDeleteRequest) -> dict[str, Any]:
    """兩步式刪除 note。uep 在 lore：第二步不真刪，改記刪除請求
    （`review_state: pending_delete`，note 照常可見），等使用者在 UI 核准才真刪；
    uep 在 dev 與其他身分照常真刪（回 `review_state: none`）。"""
    principal = principal_of(request)
    review_service.check_uep_space(principal, req.space)
    request_only = review_service.is_review_space(principal, req.space or "")
    reason = _reason(req.reason, admin.DEFAULT_NOTE_REASON)

    def plan(conn: sqlite3.Connection) -> tuple[dict[str, Any], Any]:
        result = admin.plan_note_deletion(
            conn,
            req.vault,  # type: ignore[arg-type]  # None → VaultRequired
            req.id,
            space=req.space,  # type: ignore[arg-type]  # None → SpaceRequired
        )
        shown = result.to_dict()
        row = conn.execute(
            "SELECT updated, review_state FROM notes WHERE id = ?", (req.id,)
        ).fetchone()
        if request_only:
            current = store_note(conn, req)
            review_service.assert_can_request_delete(current)
            shown["review_request"] = True
        return shown, {"plan": shown, "updated": row[0], "review": row[1]}

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        if request_only:
            note = review_service.request_delete(
                conn,
                req.vault,  # type: ignore[arg-type]
                req.id,
                space=req.space,  # type: ignore[arg-type]
            )
            return {"review_state": note.review_state}
        admin.delete_note(
            conn,
            req.vault,  # type: ignore[arg-type]
            req.id,
            space=req.space,  # type: ignore[arg-type]
            reason=reason,
        )
        return {"review_state": "none"}

    return _two_phase(
        request,
        "note_delete",
        _args(req),
        req.confirm_token,
        plan,
        execute,
        target=DeleteTarget(
            kind=proposal_types.KIND_NOTE_DELETE,
            vault=req.vault,
            space=req.space,
            target_id=req.id,
            reason=reason,
            summary=lambda conn, key: _note_summary(conn, key, req.id),
        ),
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
        request,
        "document_delete",
        _args(req),
        req.confirm_token,
        plan,
        execute,
        target=DeleteTarget(
            kind=proposal_types.KIND_DOCUMENT_DELETE,
            vault=req.vault,
            space=req.space,
            target_id=req.id,
            reason=reason,
            summary=lambda conn, key: _document_summary(conn, key, req.id),
        ),
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
    """取消刪除 note（同 CLI `undelete-note --yes`）。

    - 墓碑有內容快照（v12 起刪除的）：以原 id 與原內容還原，`restored: true`，
      `note` 附還原後的 metadata；FTS 同交易重建，向量由背景補算（喚醒 worker）。
      所屬 vault 已刪除 → 409 `not_restorable`（reason `vault_deleted`），墓碑保留
    - 舊墓碑（v12 前，無快照）：維持舊行為，只移除墓碑、內容**不會**回來
      （`restored: false`）；有匯入來源的在下次重跑匯入時匯回（`reimportable`）
    """
    state = _state(request)
    with state.connection() as conn:
        with transaction(conn):
            store.note_tombstone_in_space(conn, req.id, space=req.space)
            result = admin.restore_note(conn, req.id, space=req.space)
    grave = result["tombstone"]
    note = result["note"]
    if result["restored"]:
        state.wake_worker()
    return {
        "undeleted": grave,
        "restored": result["restored"],
        "reimportable": not result["restored"] and grave["source"] is not None,
        "note": None
        if note is None
        else {
            "id": note.id,
            "vault": note.vault,
            "title": note.title,
            "author": note.author,
            "updated": note.updated,
        },
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
            since=req.since,
            until=req.until,
            offset=req.offset,
        )
        total = (
            store.count_concepts(
                conn,
                req.vault,
                space=req.space,
                scope=req.scope,
                scope_state=req.scope_state,
                kind=req.kind,
                since=req.since,
                until=req.until,
            )
            if req.with_total
            else None
        )
    result: dict[str, Any] = {
        "items": items,
        "next_cursor": _encode_cursor(next_cursor),
    }
    if total is not None:
        result["total"] = total
    return result


@router.post("/concept_graph")
def concept_graph(request: Request, req: ConceptGraphRequest) -> dict[str, Any]:
    """單一 scope 的 concept 關聯圖（共用 anchor 推導、唯讀），欄位同 concept_query。"""
    with _state(request).connection() as conn:
        result = store.concept_graph(
            conn,
            req.vault,
            space=req.space,
            scope=req.scope,
            scope_state=req.scope_state,
            kind=req.kind,
            since=req.since,
            until=req.until,
            max_nodes=req.max_nodes,
            anchor_fanout_cap=req.anchor_fanout_cap,
        )
    return result


@router.post("/concept_scopes")
def concept_scopes(request: Request, req: ConceptScopesRequest) -> dict[str, Any]:
    """各 scope 的 concept 筆數（UI 的 scope 下拉選單；唯讀）。"""
    with _state(request).connection() as conn:
        scopes = store.concept_scopes(
            conn,
            req.vault,
            space=req.space,
            scope_state=req.scope_state,
            kind=req.kind,
        )
    return {"scopes": scopes}


@router.post("/episode_summary")
def episode_summary(request: Request, req: EpisodeSummaryRequest) -> dict[str, Any]:
    """各 machine／vault 的 episode 筆數與最近時間，不含任何對話原文。"""
    with _state(request).connection() as conn:
        summary = store.episode_summary(conn, req.vault, space=req.space)
    return {"space": req.space, "vault": req.vault, **summary}


# ── 標籤清單 ──


@router.post("/topics")
def topics(request: Request, req: TopicsRequest) -> dict[str, Any]:
    """範圍內 note 的 topic 與使用筆數；`vault="*"` 為目前 space 內全部 vault。"""
    with _state(request).connection() as conn:
        key, items = store.topic_counts(conn, req.vault, space=req.space)
    return {"space": req.space, "vault": key or req.vault, "topics": items}
