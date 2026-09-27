"""MCP 工具（A15／D12）：本地 stdio 殼與服務內建的 HTTP 端點 `/mcp` 共用這一份。

十三個工具一律轉發到服務 `/v1/*`：stdio 殼經網路打服務（不可達時讀本地快照降級）；
HTTP 端點（`mcp.http`）在服務行程內經 in-process ASGI 轉發到同一個 app，
沿用呼叫端自己的認證 header（同一套 principal 判定）。

兩種模式的工具名稱、參數、說明、回傳完全相同（`build_server` 只註冊一次）；
差異只在 `Shell` 的模式檢查（D12）：
- `vault_resolve`：HTTP 看不到客戶端檔案系統，不能用 `cwd`，改收 `remote_url`
  （客戶端 `git remote get-url origin` 的輸出，正規化規則同 binding）或 `key`
- `upload`：HTTP 只收 `filename` + `content_base64`，不收本機 `path`
- HTTP 沒有快照降級；目前 space 依 MCP session（`mcp-session-id`）各自保存

工具刻意只有 `space`、`vault_resolve`、`recall`、`ask`、`get`、`list`、`write`、
`update`、`upload`、`download`、`delete`、`undelete`、`status`；建 vault 併入
`vault_resolve(create=True)`，不另開工具。不暴露 chat／model／settings／source，
也不開放刪 vault（只留給 UI 與管理指令）。

`delete`／`undelete`：依 id 前綴分派（`doc:` → 文件、`chunk:` 拒絕、其餘 → note），
直接轉 `/v1/{note,document}_{delete,undelete}`，殼不另寫刪除邏輯。刪除沿用服務端的
兩段式確認（不帶 `confirm_token` 只規劃）；工具說明要求 agent 取得使用者同意後才送
第二步。服務不可達時三個工具都直接失敗（快照沒有 blob 與簽章祕密）。

`download`：轉 `POST /v1/document_download`（驗 sha256）。stdio 寫到 `upload_roots`
白名單內的本機路徑（規則見 `mcp.download`，既有檔預設不覆寫）；HTTP 回
`content_base64`，上限 `mcp.http_download_max_bytes`（殼帶 `max_bytes` 讓服務先擋，
殼端收的時候再擋一次）。

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

import base64
import binascii
import hashlib
import json
import logging
import os
import sqlite3
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

import anyio
import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from lore_vault import notes as notes_service
from lore_vault.binding import display_from_remote, normalize_remote, resolve_binding
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

from .client import (
    DownloadTooLarge,
    ServiceClient,
    ServiceError,
    ServiceUnreachable,
)
from .download import (
    DownloadPathError,
    resolve_download_path,
    safe_filename,
    write_download,
)
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
    "download",
    "delete",
    "undelete",
    "status",
)
SPACE_ACTIONS = ("get", "set")
DEGRADED_REASON = "service_unreachable"

SOURCE_REMOTE_URL = "remote_url"
MODE_STDIO = "stdio"
MODE_HTTP = "http"
MODES = (MODE_STDIO, MODE_HTTP)
# HTTP 模式：轉發到服務時沿用呼叫端的這些認證 header（不以服務 token 代打）
FORWARD_HEADERS = ("authorization", "cookie", "x-lore-vault-ui")
SESSION_HEADER = "mcp-session-id"
# HTTP 模式各 MCP session 的目前 space 最多記幾個（超過淘汰最久沒用的）
MAX_SESSION_SPACES = 1024
# 上傳檔名上限（字元）
MAX_UPLOAD_FILENAME = 255
# 服務的文件／chunk id 前綴（其餘 id 一律視為 note）
DOCUMENT_PREFIX = "doc:"
CHUNK_PREFIX = "chunk:"
KIND_NOTE = "note"
KIND_DOCUMENT = "document"
# 下載回應的 header（`api.routes.document_download`）
SHA256_HEADER = "x-lore-vault-sha256"
# delete 第一步（規劃）附給 agent 的下一步指示
DELETE_NEXT_STEP = (
    "尚未刪除。把 plan 給使用者看、取得明確同意後，才以相同的 vault／id／reason "
    "加上 confirm_token 再呼叫一次 delete；不要自動連打兩步。token 5 分鐘內有效，"
    "期間不要切換 space"
)

# 目前這次工具呼叫所屬的 MCP session 與要轉發的 header（`Shell.request_scope` 設定）
_session_var: ContextVar[str | None] = ContextVar("lore_mcp_session", default=None)
_forward_var: ContextVar[dict[str, str] | None] = ContextVar(
    "lore_mcp_forward", default=None
)


def forwarded_headers() -> dict[str, str] | None:
    """HTTP 模式：目前工具呼叫要轉發給服務的認證 header（`mcp.http` 用）。"""
    return _forward_var.get()


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
    "可參考但不可當作唯一事實來源，關鍵事實要用 get 核對原 note。"
    "刪除用 delete，兩步式：不帶 confirm_token 只回規劃與 token（不會刪）；必須先把"
    "規劃給使用者看、取得明確同意，才帶 token 再呼叫一次——不要自動連打兩步。"
    "刪除會留墓碑，可用 undelete 以原 id 還原。文件原始檔用 download 寫到本機"
    "（只限殼工作目錄或 mcp.upload_roots 之下，既有檔不覆寫除非 overwrite=true）。"
)

HTTP_INSTRUCTIONS = (
    "Lore Vault：專案記憶（HTTP 端點）。流程：先 vault_resolve 取得本專案的 vault key"
    " → recall 查（只回標題與摘要）→ 需要全文再 get → 新結論用 write、修正既有 note "
    "用 update（不要另建更正篇）。服務看不到你的檔案系統：vault_resolve 請帶 "
    "remote_url（在專案目錄執行 `git remote get-url origin` 的輸出原樣傳入，"
    "服務會正規化成 key）；沒有 git remote 時直接給 key"
    "（例如 'folder/<資料夾名小寫>'）；不要傳 cwd。"
    "每次讀寫都要帶 vault；跨 vault 查詢必須明示 vault='*'（只涵蓋目前 space）。"
    "內容分 space：dev（開發記憶，預設）、lore（世界觀）、personal（私人）；所有工具只"
    "看得到目前 space，要看別的 space 先用 space(action='set') 切換（依 MCP session "
    "保存，新 session 一律回到 dev）。文件用 upload 上傳：傳 filename 與 content_base64"
    "（檔案內容 base64），不收本機路徑；上傳後在背景抽取，recall 會一併回文件段落"
    "（kind=chunk），全文用 get 取 doc:／chunk: id。HTTP 端點沒有離線快照：服務連不上"
    "時工具直接失敗。ask 會把 recall 到的 note 交模型整理成逐點回答；那只是片段的整理、"
    "信心有限，關鍵事實要用 get 核對原 note。"
    "刪除用 delete，兩步式：不帶 confirm_token 只回規劃與 token（不會刪）；必須先把"
    "規劃給使用者看、取得明確同意，才帶 token 再呼叫一次——不要自動連打兩步。"
    "刪除會留墓碑，可用 undelete 以原 id 還原。download 在 HTTP 端點以 "
    "content_base64 回傳原始檔（有大小上限，超過請改用本地 stdio 殼或 UI 下載）。"
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
    "path_not_supported": (
        "HTTP 端點讀不到你的檔案系統：改傳 filename 與 content_base64"
        "（檔案內容 base64）"
    ),
    "cwd_not_supported": (
        "HTTP 端點看不到你的工作目錄：在專案目錄執行 `git remote get-url origin`，"
        "把輸出當 remote_url 傳入；沒有 remote 時直接給 key（'folder/<資料夾名小寫>'）"
    ),
    "remote_url_required": (
        "HTTP 端點需要 remote_url（`git remote get-url origin` 的輸出）或 key"
    ),
    "session_required": (
        "這個連線沒有 MCP session（stateless），無法保存目前 space；"
        "請用支援 session 的 streamable HTTP 客戶端，或只在 dev 使用"
    ),
    "too_large": "單檔上限 25MB（設定 documents.max_file_bytes）",
    "ask_not_configured": "服務沒有問答模型（缺 OPENAI_API_KEY）；改用 recall + get",
    "ask_disabled": "問答已由管理者在服務設定頁關閉；改用 recall + get",
    "ask_provider_error": "問答模型呼叫失敗；稍後重試，或改用 recall + get",
    "ask_timeout": "問答模型逾時；稍後重試、降低 k，或改用 recall + get",
    "ask_rate_limited": "問答模型被限流；等 retry_after 秒後重試，或改用 recall + get",
    "ask_invalid_output": (
        "模型輸出不合格（空、截斷或格式錯）；重試一次，仍失敗改用 recall + get"
    ),
    "unsupported_format": (
        "支援 md、txt（含程式碼等純文字）、json、yaml、toml、pdf、docx、pptx"
    ),
    "invalid_confirm_token": (
        "confirm_token 必須搭配規劃時完全相同的 vault／id／reason（與同一個 space）"
        "原樣送回；不確定就不帶 token 重新規劃，給使用者確認後再送"
    ),
    "confirm_token_expired": "token 已過期（5 分鐘）：重新規劃並再次取得使用者同意",
    "plan_changed": (
        "規劃後資料已變動、這次未刪除：把錯誤附的新 plan 給使用者看，同意後才以"
        "附帶的新 confirm_token 重送"
    ),
    "not_restorable": (
        "墓碑無法還原（見 reason：vault 已刪除、原始檔遺失、同內容已重新上傳等）"
    ),
    "not_found": (
        "id 不在目前 space／vault 內；確認 id，或先用 space(action='set') 切換"
    ),
    "file_exists": "目的檔已存在：換一個 path，或確認後以 overwrite=true 覆寫",
    "parent_not_found": "目的目錄不存在；下載不會自動建目錄",
    "blob_missing": (
        "服務端原始檔遺失；請使用者執行 doctor 檢查 documents.blob_exists"
    ),
    "blob_corrupt": (
        "服務端原始檔雜湊不符；請使用者執行 doctor 檢查 documents.blob_exists"
    ),
    "hash_mismatch": "收到的內容與服務宣告的 sha256 不符，未寫入；重試一次",
}

DOWNLOAD_TOO_LARGE_HINTS = {
    MODE_HTTP: (
        "HTTP 端點以 base64 回傳、有大小上限（mcp.http_download_max_bytes）："
        "改用本地 stdio 殼的 download（直接寫本機檔），或請使用者從 UI 下載"
    ),
    MODE_STDIO: "超過殼端上限（documents.max_file_bytes）；請使用者從 UI 下載",
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


def _ctx_headers(ctx: Context | None) -> Mapping[str, str] | None:
    """工具呼叫所在 HTTP 請求的 header；stdio、in-memory 或不在請求內時為 None。"""
    if ctx is None:
        return None
    try:
        return ctx.headers
    except ValueError:  # 直接呼叫 server.call_tool()：沒有 request context
        return None


def _decode_upload(
    filename: str, content_base64: str, max_bytes: int
) -> tuple[str, bytes]:
    """內容上傳（HTTP 模式唯一方式，stdio 也可用）：檢查檔名、先以長度擋大小再解碼。"""
    name = filename.strip() if isinstance(filename, str) else ""
    if not name:
        raise _tool_error("invalid_request", "filename 不可為空")
    if (
        len(name) > MAX_UPLOAD_FILENAME
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        raise _tool_error(
            "invalid_request",
            "filename 只能是單純檔名（不含路徑分隔與控制字元，最多 "
            f"{MAX_UPLOAD_FILENAME} 字）：{name!r}",
        )
    text = "".join(content_base64.split()) if isinstance(content_base64, str) else ""
    # base64 每 4 字元 3 位元組：先以長度擋，不為超大內容配置解碼緩衝
    if len(text) > (max_bytes + 2) // 3 * 4:
        raise _tool_error(
            "too_large",
            f"{name!r} 超過上傳上限 {max_bytes} 位元組",
            hint=_HINTS["too_large"],
        )
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise _tool_error(
            "invalid_request", "content_base64 不是合法的 base64"
        ) from None
    if len(data) > max_bytes:
        raise _tool_error(
            "too_large",
            f"{name!r} 超過上傳上限 {max_bytes} 位元組",
            hint=_HINTS["too_large"],
        )
    return name, data


def _disposition_filename(value: str | None) -> str | None:
    """`Content-Disposition` 的檔名：優先 RFC 5987 的 `filename*=UTF-8''…`。"""
    if not value:
        return None
    for part in value.split(";"):
        key, _, raw = part.strip().partition("=")
        if key.lower() == "filename*" and raw.lower().startswith("utf-8''"):
            return unquote(raw[len("utf-8''") :])
    for part in value.split(";"):
        key, _, raw = part.strip().partition("=")
        if key.lower() == "filename":
            return raw.strip('"')
    return None


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
        mode: str = MODE_STDIO,
        download_limit: Callable[[], int] | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode 必須是 {MODES} 之一，得到 {mode!r}")
        self.settings = settings
        self.mode = mode
        # `download` 上限的即時來源（HTTP 端點由服務的執行期設定提供，D13）；
        # None = 用 settings.download_max_bytes
        self._download_limit = download_limit
        self.client = ServiceClient(settings, transport=transport)
        self._cwd = cwd
        self._now = now
        self._pull_lock = anyio.Lock()
        self.last_pull_error: str | None = None
        self._concept_lock = anyio.Lock()
        self.last_concept_pull_error: str | None = None
        # 目前 space：只在記憶體，不持久化（新殼行程／新 MCP session 一律 dev）。
        # stdio 只有一格（key None）；HTTP 依 MCP session id 分格
        self._spaces: OrderedDict[str | None, str] = OrderedDict()

    @property
    def http(self) -> bool:
        return self.mode == MODE_HTTP

    @property
    def space(self) -> str:
        return self._spaces.get(_session_var.get(), SPACE_DEV)

    @space.setter
    def space(self, value: str) -> None:
        key = _session_var.get()
        self._spaces[key] = value
        self._spaces.move_to_end(key)
        while len(self._spaces) > MAX_SESSION_SPACES:
            self._spaces.popitem(last=False)

    @contextmanager
    def request_scope(self, ctx: Context | None) -> Iterator[None]:
        """一次工具呼叫的範圍：HTTP 模式記下 MCP session 與要轉發的認證 header。"""
        if not self.http:
            yield
            return
        headers = _ctx_headers(ctx)
        session_id = headers.get(SESSION_HEADER) if headers is not None else None
        forward = None
        if headers is not None:
            forward = {
                name: value
                for name in FORWARD_HEADERS
                if (value := headers.get(name)) is not None
            }
        session_token = _session_var.set(session_id or None)
        forward_token = _forward_var.set(forward)
        try:
            yield
        finally:
            _forward_var.reset(forward_token)
            _session_var.reset(session_token)

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
        if self.http:
            raise _tool_error(
                DEGRADED_REASON,
                f"服務內部不可用（{cause.detail}）；HTTP 端點沒有快照降級",
                hint="稍後重試",
            )
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
        if self.http and _session_var.get() is None:
            # 沒有 session 就沒有「目前」可言：寫進共用格會改到別的 agent 的 space
            raise _tool_error(
                "session_required",
                "這個 MCP 連線沒有 session，無法切換目前 space",
                hint=_HINTS["session_required"],
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
        remote_url: str | None = None,
    ) -> dict[str, Any]:
        """dev：key 省略時由 remote_url（或 stdio 的 cwd）的 binding 算；
        lore／personal：必須帶 key、忽略 cwd／remote_url。

        `remote_url` 以與 binding 相同的 `normalize_remote` 正規化，與在該 repo
        目錄以 cwd 解析得到同一個 key。HTTP 模式不能用 cwd（服務看不到客戶端）。
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
        elif remote_url is not None and target == SPACE_DEV:
            normalized = normalize_remote(remote_url)
            if not normalized:
                raise _tool_error(
                    "invalid_request",
                    "remote_url 不可為空",
                    hint=_HINTS["remote_url_required"],
                )
            bind = {
                "key": normalized,
                "display": display_from_remote(remote_url) or normalized,
                "source": SOURCE_REMOTE_URL,
            }
            resolved_key, default_display = normalized, bind["display"]
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
        elif self.http:
            if cwd is not None:
                raise _tool_error(
                    "cwd_not_supported",
                    "HTTP 端點不能用 cwd 推算 vault（服務看不到客戶端的檔案系統）",
                    hint=_HINTS["cwd_not_supported"],
                )
            raise _tool_error(
                "remote_url_required",
                "HTTP 端點必須帶 remote_url 或 key",
                hint=_HINTS["cwd_not_supported"],
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
                        "display=...) 建立；若 cwd／remote_url 不是這個專案的，"
                        "改傳正確的值；若 vault 在別的 space，先用 "
                        "space(action='set') 切換"
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

    async def upload(
        self,
        path: str | None = None,
        vault: str | None = None,
        filename: str | None = None,
        content_base64: str | None = None,
    ) -> dict[str, Any]:
        """上傳到目前 space 的 vault：本機檔案（`path`，只限 stdio）或內容
        （`filename` + `content_base64`，兩種模式皆可）二擇一。

        `vault` 省略時只在 stdio 的 dev 以殼工作目錄的 binding 解析（不建立 vault），
        並在回應標 `vault_source: "cwd_binding"`；lore／personal 與 HTTP 必須明示。
        """
        has_content = filename is not None or content_base64 is not None
        if path is not None and has_content:
            raise _tool_error(
                "invalid_request", "path 與 filename／content_base64 只能擇一"
            )
        if path is not None and self.http:
            raise _tool_error(
                "path_not_supported",
                "HTTP 端點不收本機路徑",
                hint=_HINTS["path_not_supported"],
            )
        source_path: str | None = None
        if path is None:
            if filename is None or content_base64 is None:
                raise _tool_error(
                    "invalid_request",
                    "必須帶 filename 與 content_base64"
                    + ("" if self.http else "（或本機 path）"),
                )
            name, data = _decode_upload(
                filename, content_base64, self.settings.max_upload_bytes
            )
        else:
            try:
                local = read_upload(
                    path,
                    self.upload_roots(),
                    cwd=self._cwd(),
                    max_bytes=self.settings.max_upload_bytes,
                )
            except UploadPathError as exc:
                raise _tool_error(
                    exc.code, str(exc), hint=_HINTS.get(exc.code)
                ) from None
            name, data, source_path = local.name, local.data, str(local.path)
        vault_source = "explicit"
        if vault is None:
            if self.http:
                raise _tool_error(
                    "vault_required",
                    "HTTP 端點的 upload 必須帶 vault（服務看不到工作目錄）",
                    hint="先用 vault_resolve(remote_url=...) 取得 key",
                )
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
                filename=name,
                content=data,
            )
        except ServiceError as exc:
            raise _from_service_error(exc) from None
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None
        result["vault_source"] = vault_source
        if source_path is not None:
            result["path"] = source_path
        return result

    # ── 刪除／還原／下載 ──

    @staticmethod
    def _item_kind(item_id: str, action: str) -> str:
        """依 id 前綴分派：`doc:` → 文件；`chunk:` 拒絕（段落不能單獨操作）；
        其餘 → note。"""
        if not isinstance(item_id, str) or not item_id.strip():
            raise _tool_error("invalid_request", "id 不可為空")
        if item_id.startswith(CHUNK_PREFIX):
            raise _tool_error(
                "invalid_request",
                f"chunk id 不能單獨{action}；請改用所屬文件的 doc: id",
            )
        return KIND_DOCUMENT if item_id.startswith(DOCUMENT_PREFIX) else KIND_NOTE

    async def delete(
        self,
        vault: str,
        id: str,
        reason: str | None = None,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """兩步式刪除（轉 `/v1/note_delete`／`/v1/document_delete`）：
        不帶 token 只規劃。

        刪除邏輯全在服務端；殼只分派與附下一步指示。服務不可達時直接失敗（不降級）。
        """
        kind = self._item_kind(id, "刪除")
        path = "/v1/document_delete" if kind == KIND_DOCUMENT else "/v1/note_delete"
        body = _compact(vault=vault, id=id, reason=reason, confirm_token=confirm_token)
        try:
            result = await self._post(path, body)
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None
        result["kind"] = kind
        if not result.get("executed"):
            result["next_step"] = DELETE_NEXT_STEP
        else:
            result["undelete_hint"] = "墓碑已寫入；需要還原時用 undelete(id=...)"
        return result

    async def undelete(self, id: str) -> dict[str, Any]:
        """以墓碑還原（轉 `/v1/note_undelete`／`/v1/document_undelete`），
        範圍為目前 space。"""
        kind = self._item_kind(id, "還原")
        path = "/v1/document_undelete" if kind == KIND_DOCUMENT else "/v1/note_undelete"
        try:
            result = await self._post(path, {"id": id})
        except ServiceUnreachable as exc:
            raise self._write_unreachable(exc) from None
        result["kind"] = kind
        return result

    async def download(
        self,
        vault: str,
        id: str,
        path: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """取回文件原始檔（`/v1/document_download`）。stdio：寫到白名單內的本機路徑；
        HTTP：回 `content_base64`（上限 `download_max_bytes`）。"""
        if self._item_kind(id, "下載") != KIND_DOCUMENT:
            raise _tool_error(
                "invalid_request",
                "download 只接受文件 id（doc:…）；note 全文用 get",
            )
        if self.http and path is not None:
            raise _tool_error(
                "path_not_supported",
                "HTTP 端點不能寫你的檔案系統；省略 path，內容以 content_base64 回傳",
                hint=DOWNLOAD_TOO_LARGE_HINTS[MODE_HTTP],
            )
        if not self.http and path is not None:
            # 先擋不允許的路徑，不為注定寫不了的請求下載內容
            self._download_target(path, "placeholder")
        limit = (
            self._download_limit()
            if self._download_limit is not None
            else self.settings.download_max_bytes
        )
        try:
            data, headers = await self.client.post_download(
                "/v1/document_download",
                {"vault": vault, "id": id, "max_bytes": limit, "space": self.space},
                max_bytes=limit,
            )
        except ServiceError as exc:
            if exc.code == "too_large":
                raise _tool_error(
                    "too_large",
                    exc.message,
                    hint=DOWNLOAD_TOO_LARGE_HINTS[self.mode],
                    http_status=exc.status,
                    limit_bytes=limit,
                ) from None
            raise _from_service_error(exc) from None
        except DownloadTooLarge as exc:
            raise _tool_error(
                "too_large",
                str(exc),
                hint=DOWNLOAD_TOO_LARGE_HINTS[self.mode],
                limit_bytes=limit,
            ) from None
        except ServiceUnreachable as exc:
            raise _tool_error(
                DEGRADED_REASON,
                f"服務不可達（{exc.detail}），download 需要服務端原始檔、無法降級",
                hint="服務恢復後重試",
            ) from None
        sha = hashlib.sha256(data).hexdigest()
        expected = headers.get(SHA256_HEADER)
        if expected != sha:
            raise _tool_error(
                "hash_mismatch",
                f"內容 sha256 {sha} 與服務宣告的 {expected!r} 不符，未寫入",
                hint=_HINTS["hash_mismatch"],
            )
        filename = _disposition_filename(headers.get("content-disposition"))
        meta: dict[str, Any] = {
            "document_id": id,
            "filename": filename,
            "mime": headers.get("content-type"),
            "size_bytes": len(data),
            "sha256": sha,
        }
        if self.http:
            meta["content_base64"] = base64.b64encode(data).decode("ascii")
            return meta
        target = self._download_target(path, safe_filename(filename, id))
        try:
            overwritten = write_download(target, data, overwrite=overwrite)
        except DownloadPathError as exc:
            raise _tool_error(exc.code, str(exc), hint=_HINTS.get(exc.code)) from None
        meta["path"] = str(target)
        meta["overwritten"] = overwritten
        return meta

    def _download_target(self, path: str | None, filename: str) -> Path:
        try:
            return resolve_download_path(
                path, self.upload_roots(), cwd=self._cwd(), filename=filename
            )
        except DownloadPathError as exc:
            hint = _HINTS.get(exc.code)
            if exc.code == "path_not_allowed":
                hint = (
                    "只能寫到殼工作目錄或設定 mcp.upload_roots 底下；"
                    "換一個目錄，或請使用者加入白名單"
                )
            raise _tool_error(exc.code, str(exc), hint=hint) from None

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
        if self.http:
            # HTTP 端點沒有殼端快照；附模式與目前 space
            result = await self._post("/v1/status", _compact(vault=vault))
            result["mcp"] = {"mode": MODE_HTTP, "space": self.space}
            return result
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
    """註冊十三個工具。stdio 與 HTTP 共用同一份定義；只有 instructions 與 lifespan
    依 `shell.mode` 不同（HTTP 沒有快照背景工作）。"""

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        try:
            async with anyio.create_task_group() as tg:
                if (
                    not shell.http
                    and shell.settings.snapshot_on_start
                    and (
                        shell.settings.snapshot_dir
                        or shell.settings.concept_snapshot_path
                    )
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
        instructions=HTTP_INSTRUCTIONS if shell.http else INSTRUCTIONS,
        version="0.1.1",
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(shell.space_tool(action, value))

    async def vault_resolve(
        cwd: Annotated[
            str | None,
            Field(
                description="專案目錄（只用於 dev、只限本地 stdio 殼）；省略時用殼"
                "啟動時的工作目錄（通常是專案根）。HTTP 端點不接受，改傳 remote_url"
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
                description="直接指定 vault key（不經 cwd／remote_url 推算）。"
                "lore／personal 必填，且必須以 '<space>/' 開頭，如 'lore/aeswir-arc'"
            ),
        ] = None,
        remote_url: Annotated[
            str | None,
            Field(
                description="專案的 git remote（在專案目錄執行 `git remote get-url "
                "origin` 的輸出原樣傳入；只用於 dev），服務依與 cwd 相同的規則正規化"
                "成 key。HTTP 端點用這個取代 cwd"
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(
                await shell.vault_resolve(cwd, create, display, space, key, remote_url)
            )

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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
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
            str | None,
            Field(
                description="本機檔案路徑（只限本地 stdio 殼；絕對，或相對於殼的工作"
                "目錄）；必須在殼工作目錄或 mcp.upload_roots 之下，不可含 '..'。"
                "與 filename／content_base64 擇一"
            ),
        ] = None,
        vault: Annotated[
            str | None,
            Field(
                description="vault key（單一 vault）。stdio 的 dev 省略時用殼工作目錄 "
                "binding；lore／personal 與 HTTP 端點必填"
            ),
        ] = None,
        filename: Annotated[
            str | None,
            Field(
                description="上傳內容的檔名（不含路徑，副檔名決定格式）；"
                "與 content_base64 一起用，HTTP 端點只能用這個方式"
            ),
        ] = None,
        content_base64: Annotated[
            str | None,
            Field(description="檔案內容的 base64（標準字母表）；與 filename 一起用"),
        ] = None,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(await shell.upload(path, vault, filename, content_base64))

    async def download(
        vault: Annotated[
            str,
            Field(description="vault key（文件所在的 vault；'*' 為目前 space 全部）"),
        ],
        id: Annotated[str, Field(description="文件 id（doc:…）")],
        path: Annotated[
            str | None,
            Field(
                description="只限本地 stdio 殼：寫入的本機路徑（檔案或既有目錄；絕對，"
                "或相對於殼的工作目錄），必須在殼工作目錄或 mcp.upload_roots 之下，"
                "不可含 '..'；省略時以原檔名寫到殼工作目錄。HTTP 端點不接受"
            ),
        ] = None,
        overwrite: Annotated[
            bool,
            Field(description="目的檔已存在時是否覆寫；預設 false（已存在就回錯誤）"),
        ] = False,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(await shell.download(vault, id, path, overwrite))

    async def delete(
        vault: Annotated[str, Field(description="vault key（單一 vault，不可 '*'）")],
        id: Annotated[
            str,
            Field(description="要刪的 note id，或文件 id（doc:…）；chunk id 不接受"),
        ],
        reason: Annotated[
            str | None, Field(description="刪除原因（記在墓碑）；省略用預設")
        ] = None,
        confirm_token: Annotated[
            str | None,
            Field(
                description="第二步才帶：第一步回傳的 confirm_token。只有在使用者看過"
                "規劃並明確同意後才可帶上；其餘參數必須與第一步完全相同"
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(await shell.delete(vault, id, reason, confirm_token))

    async def undelete(
        id: Annotated[
            str,
            Field(description="已刪除的 note id 或文件 id（doc:…），同刪除時的 id"),
        ],
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(await shell.undelete(id))

    async def status(
        vault: Annotated[
            str | None, Field(description="另附該 vault 的筆數與最近更新")
        ] = None,
        ctx: Context | None = None,
    ) -> str:
        with shell.request_scope(ctx):
            return _dump(await shell.status(vault))

    descriptions = {
        "space": (
            "查詢或切換「目前 space」：dev（開發記憶，預設）、lore（世界觀）、"
            "personal（私人）。其他工具只看得到目前 space 的內容（vault='*' 也只涵蓋"
            "目前 space）。只在記憶體中（本地殼依行程、HTTP 端點依 MCP session），"
            "新 session 一律回到 dev。"
        ),
        "vault_resolve": (
            "取得 vault key。dev：由專案的 git remote 算出（自動解析改名別名）——"
            "本地 stdio 殼可用 cwd（省略時用殼的工作目錄）；HTTP 端點看不到你的檔案"
            "系統，改傳 remote_url（`git remote get-url origin` 的輸出），沒有 remote "
            "時直接給 key。lore／personal：沒有 repo，必須帶 key（'<space>/名稱'），"
            "cwd／remote_url 會被忽略。每個 session 開始時先呼叫一次，之後所有工具都帶"
            "回傳的 key。vault 不存在會回錯誤；確認要建記憶時才用 create=true 建立。"
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
            "信心有限——片段沒撈到的不會知道，模型也可能因輸出抖動或極端情況整理出"
            "不精確的內容。結果可以參考，但不要當作唯一事實來源；關鍵事實請以 get"
            "核對原 note 再採用。"
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
            "兩種給法擇一：path（只限本地 stdio 殼，只能讀殼工作目錄或 "
            "mcp.upload_roots 之下的檔案），或 filename + content_base64（HTTP 端點"
            "只能用這個）。服務不可達時直接失敗。"
        ),
        "download": (
            "取回已上傳文件的原始檔（上傳時的位元組，服務端驗過 sha256）。"
            "本地 stdio 殼：寫到本機 path（只限殼工作目錄或 mcp.upload_roots 之下；"
            "目的檔已存在時預設拒絕，要覆寫須 overwrite=true），回寫入的 path、"
            "filename、mime、size_bytes、sha256。HTTP 端點：不收 path，回 "
            "content_base64 與同樣的 metadata；超過大小上限（預設 1MB）回 too_large，"
            "請改用 stdio 殼或 UI 下載。只看得到目前 space；已刪除（墓碑中）的文件要先 "
            "undelete。服務不可達時直接失敗。"
        ),
        "delete": (
            "刪除一則 note 或一份文件（id 以 doc: 開頭為文件，其餘為 note）。兩步式："
            "(1) 不帶 confirm_token → 只回 plan 與 confirm_token（唯讀，不會刪）；"
            "(2) 把 plan 給使用者看、取得明確同意後，才以完全相同的參數加上 "
            "confirm_token 再呼叫一次才真正刪除。不要自動連打兩步；"
            "token 5 分鐘內有效，資料在兩步之間變動會回 plan_changed"
            "（附新 plan 與新 token，仍需使用者再確認）。"
            "刪除會寫墓碑，可用 undelete 還原；文件的原始檔不會被刪。不能刪整個 vault。"
        ),
        "undelete": (
            "從墓碑還原先前刪除的 note 或文件（同一個 id，範圍為目前 space）。"
            "note 以原內容還原（v12 前的舊墓碑沒有內容快照，只移除墓碑，"
            "見 restored）；文件以仍在的原始檔重建並重新排入抽取（status 回 "
            "pending）。vault 已刪除、原始檔遺失或同內容已重新上傳時回 "
            "not_restorable。"
        ),
        "status": (
            "服務健康狀態（doctor 對帳、補算積壓、schema 版本）。本地 stdio 殼另附"
            "本地快照狀態（shell），服務不可達時仍回傳殼端狀態並標 degraded；"
            "HTTP 端點另附 mcp（模式與目前 space）。"
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
        ("download", download),
        ("delete", delete),
        ("undelete", undelete),
        ("status", status),
    ):
        # 非結構化輸出：只回一份緊湊 JSON 文字，不重複送 structuredContent
        server.add_tool(
            fn, name=name, description=descriptions[name], structured_output=False
        )
    return server
