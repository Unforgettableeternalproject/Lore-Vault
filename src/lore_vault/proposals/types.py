"""提案的型別、狀態常數與錯誤（純標準庫、無 I/O）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 種類與狀態 ──
KIND_NOTE_DELETE = "note_delete"
KIND_DOCUMENT_DELETE = "document_delete"
KIND_NOTE_UPDATE = "note_update"
KIND_LINK_CHANGE = "link_change"
KIND_TASK_ARCHIVE = "task_archive"
KINDS = (
    KIND_NOTE_DELETE,
    KIND_DOCUMENT_DELETE,
    KIND_NOTE_UPDATE,
    KIND_LINK_CHANGE,
    KIND_TASK_ARCHIVE,
)
TARGET_NOTE = "note"
TARGET_DOCUMENT = "document"
TARGET_TASK_CHANGE = "task_change"
TARGET_OF_KIND = {
    KIND_NOTE_DELETE: TARGET_NOTE,
    KIND_DOCUMENT_DELETE: TARGET_DOCUMENT,
    KIND_NOTE_UPDATE: TARGET_NOTE,
    KIND_LINK_CHANGE: TARGET_NOTE,
    KIND_TASK_ARCHIVE: TARGET_TASK_CHANGE,
}

PENDING = "pending"
APPROVED = "approved"
EXECUTING = "executing"
APPLIED = "applied"
REJECTED = "rejected"
SUPERSEDED = "superseded"
EXPIRED = "expired"
OBSOLETE = "obsolete"
STATES = (
    PENDING,
    APPROVED,
    EXECUTING,
    APPLIED,
    REJECTED,
    SUPERSEDED,
    EXPIRED,
    OBSOLETE,
)
OPEN_STATES = (PENDING, APPROVED, EXECUTING)
TERMINAL_STATES = (APPLIED, REJECTED, SUPERSEDED, EXPIRED, OBSOLETE)
# 佇列預設只列等人處理的（approved 的 task_archive 仍待殼執行，可見）
QUEUE_DEFAULT_STATES = (PENDING, APPROVED)

VIA_UI = "ui"
VIA_TOKEN = "token"
VIA_UI_DIRECT = "ui_direct"
VIA_SYSTEM = "system"

# 預設 14 天過期、每個 vault 最多 200 筆開放提案、殼的執行租約 5 分鐘
DEFAULT_TTL_DAYS = 14
MAX_OPEN_PER_VAULT = 200
CLAIM_LEASE_SECONDS = 300

SPACES = ("dev", "lore")


# ── 寫入者 ──
ACTOR_UI = "ui"
ACTOR_AGENT = "agent"
ACTOR_SYSTEM = "system"
ACTOR_APPROVED = "approved"


@dataclass(frozen=True)
class Actor:
    """這次寫入是誰做的。`kind` 由認證方式決定（UI session → ui、bearer → agent），
    絕不由 principal 判斷。

    - `ui`：UI session，寫入來源為 ui，不送審
    - `agent`：bearer，受提案政策約束，寫入來源為 agent
    - `system`：CLI／匯入／補算等內部流程，不送審，寫入來源為 agent（保守，不追溯）
    - `approved`：套用「已核准的提案」，不送審，寫入來源為 ui（使用者已審視）；
      principal／author 仍記提案者，不假冒使用者
    """

    kind: str
    principal: str
    author: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in (ACTOR_UI, ACTOR_AGENT, ACTOR_SYSTEM, ACTOR_APPROVED):
            raise ValueError(f"Actor.kind 不合法：{self.kind!r}")
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise TypeError("principal 必填（由服務依憑證判定，不可由請求指定）")

    @classmethod
    def ui(cls, principal: str, author: str | None = None) -> Actor:
        return cls(ACTOR_UI, principal, author)

    @classmethod
    def agent(cls, principal: str, author: str | None = None) -> Actor:
        return cls(ACTOR_AGENT, principal, author)

    @classmethod
    def system(cls, principal: str, author: str | None = None) -> Actor:
        return cls(ACTOR_SYSTEM, principal, author)

    @classmethod
    def system_apply(cls, principal: str, author: str | None = None) -> Actor:
        return cls(ACTOR_APPROVED, principal, author)

    @property
    def write_origin(self) -> str:
        return "ui" if self.kind in (ACTOR_UI, ACTOR_APPROVED) else "agent"


# ── 提案列 ──
@dataclass(frozen=True)
class ProposalRow:
    id: str
    space: str
    vault: str
    kind: str
    target_type: str
    target_id: str
    payload: dict[str, Any]
    payload_digest: str
    base_updated: str | None
    base_snapshot: dict[str, Any] | None
    proposer_principal: str
    proposer_author: str | None
    state: str
    rev: int
    created_at: str
    updated_at: str
    expires_at: str
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolved_via: str | None = None
    resolution_reason: str | None = None
    superseded_by: str | None = None
    claim_id: str | None = None
    claim_expires_at: str | None = None
    claim_prev_state: str | None = None
    applied_updated: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def brief(self) -> dict[str, Any]:
        """給 bearer 看的精簡形狀：不含 payload、base_snapshot（含筆記原文）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "base_updated": self.base_updated,
            "expires_at": self.expires_at,
        }

    def to_admin(self) -> dict[str, Any]:
        """給 UI 看的完整形狀（UI session 專用）。"""
        return {
            "id": self.id,
            "space": self.space,
            "vault": self.vault,
            "kind": self.kind,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "state": self.state,
            "rev": self.rev,
            "payload": self.payload,
            "base_snapshot": self.base_snapshot,
            "base_updated": self.base_updated,
            "proposer": {
                "principal": self.proposer_principal,
                "author": self.proposer_author,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "resolved_via": self.resolved_via,
            "resolution_reason": self.resolution_reason,
            "superseded_by": self.superseded_by,
        }


# ── 政策決定 ──
@dataclass(frozen=True)
class Decision:
    """`policy.decide_*` 的結果。hold=False：照常寫入。hold=True：建立 `kind` 提案，
    `payload` 為提案內容；`partial` 時其餘欄位（`remaining`）仍立即套用。"""

    hold: bool
    kind: str | None = None
    payload: dict[str, Any] | None = None
    partial: bool = False
    # partial 時仍要套用的變更（已去掉被攔下的部分）
    remaining: dict[str, Any] | None = None


NO_HOLD = Decision(hold=False)


# ── 錯誤 ──
class ProposalError(Exception):
    """提案層錯誤；`code` 為對外錯誤碼、`status` 為 HTTP 狀態碼。"""

    code = "proposal_error"
    status = 400

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


class ProposalNotFound(ProposalError):
    code, status = "proposal_not_found", 404


class ProposalStateConflict(ProposalError):
    """rev 或狀態不符（提案已被處理或有更新）。"""

    code, status = "proposal_state_conflict", 409


class ProposalResolved(ProposalError):
    """已被另一個通道結案（claim 輸了、token 與 UI 核准競爭等）。"""

    code, status = "proposal_resolved", 409


class ProposalExecuting(ProposalError):
    code, status = "proposal_executing", 409


class ProposalStale(ProposalError):
    """核准時被觸及欄位已與提出時的基準不同；附三份內容供 UI 三欄比對。"""

    code, status = "proposal_stale", 409


class ProposalQueueFull(ProposalError):
    code, status = "proposal_queue_full", 429


class ProposalKindForbidden(ProposalError):
    code, status = "proposal_kind_forbidden", 403
