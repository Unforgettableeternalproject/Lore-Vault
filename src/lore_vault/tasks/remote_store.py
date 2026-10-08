"""任務層服務端內容的讀寫（TASK_LAYER_MCP §1.1～1.2）：change 全文、索引、主 spec 鏡像、
授權紀錄，全部存在核心的版本化側載（`/v1/blob_put`／`/v1/blob_get`，`task-` 前綴、
1MB 上限、受 `tasks.remote_sync` 開關管制）。MCP 的 `tasks` 工具與（MCP-T6 起）
本機 CLI 共用本模組；傳輸只要一個 async `post(path, body) -> dict`
（`Post`），由呼叫端注入：MCP 走 `Shell._send`，CLI 用 `vault_client_post`。
任務層只用 dev space：每個請求都顯式帶 `space: "dev"`，不受 MCP 目前 space 影響。

## 服務端資料格式（全部 `application/json`，UTF-8，鍵排序固定）

`task-change:<name>`：一個 change 的全部工作內容，**單一版本號**（側載 `version`）::

    {"schema": 1, "name": str,
     "state": "active" | "pending_apply",
     "meta": {.openspec.yaml 的欄位（不含本機同步欄位 remote_version／remote_digest）},
     "proposal_md": str, "design_md": str | null, "tasks_md": str,
     "deltas": {capability: spec delta 全文},
     "apply": null | {"archived_at": "YYYY-MM-DDTHH:MM:SSZ",
                      "merged_specs": {capability: 併入後的主 spec 全文},
                      "mirror_versions": {capability: 段一推進後的鏡像版本}}}

- `state: "pending_apply"`：archive 段一已完成（note 已寫、鏡像已推進），本機
  `specs/` 尚未落地。`apply.merged_specs` 是段二（`sync_specs`，MCP-T6）要寫回本機
  `specs/<cap>/spec.md` 的全文（沿用原檔行尾）；`skip_specs` 的 change 為 `{}`
- 段一寫入的 meta 欄位：`vault`、`notes`、`note_digests`、`note_id`、`authorized_by`、
  `authorization`（核准時的版本，見下）、`incomplete_at_archive`、`archive_reason`、
  `archived_at`、`mirror_applying`／`mirror_applied_caps`（鏡像推進的 write-ahead，
  語意同本機的 `spec_applying`／`spec_applied_caps`，刻意用不同鍵名）

`task-index`：vault 內 change 的列舉（服務端沒有「依前綴列 key」的端點）::

    {"schema": 1, "changes": {name: {"state": "active" | "pending_apply" | "archived"}}}

- 只負責列舉；狀態以 change 文件本身的 `state` 為準（兩者不一致時信 change 文件）
- propose 先以 `expected_version=0` 建 change（保證名稱唯一），再以 CAS 補進索引；
  補索引失敗時 change 已存在但列不到，重跑 propose 會回 `change_exists` 並補上索引
- `archived`：段二落地後（MCP-T6）或遷移舊封存（MCP-T7）時寫入，供 `depends_on` 判定

`task-spec-mirror:<capability>`：主 spec 的唯讀鏡像（git 才是權威）::

    {"schema": 1, "capability": str, "exists": bool, "text": str | null,
     "source": str}

- `exists: false`：本機確實沒有這個 capability 的主 spec（新 capability）；
  與「沒有鏡像」（key 不存在）區分——沒有鏡像時段一一律拒絕，不會用 skeleton 蓋掉
  真實的主 spec
- `source`：`"stdio"`（stdio 的 init／validate 從本機檔案推送）或
  `"archive:<change>"`（archive 段一推進成併入後內容，尚未落地）
- stdio 推送時跳過有 `pending_apply` change 正在合併的 capability，避免本機舊內容把
  段一推進的鏡像倒退

`task-authorization:<name>`：`requires_authorization` 的人類核准紀錄（§3.3）::

    {"schema": 1, "vault": 正式 key, "change": name, "change_version": int,
     "authorized_by": str, "authorized_at": "YYYY-MM-DDTHH:MM:SSZ",
     "principal": {"kind": "ui_session", "name": str}}

- **本模組只讀不寫**（`put_blob` 拒絕此前綴）；寫入端點與 UI 按鈕是 MCP-T5：
  只收 UI session（cookie＋`X-Lore-Vault-UI`），`principal` 由服務端依認證填，
  且 `/v1/blob_put` 必須拒絕非 UI session 寫入此前綴（在那之前持 bearer 者可偽造）
- `change_version`：核准當下的 change 版本；之後再 edit 即失效
  （`authorization_stale`）。
  archive 開始時把紀錄抄進 meta `authorization`，續跑以它比對、不因自身寫入的版本遞增
  而失效

## 本機工作副本（stdio）

`openspec/changes/<name>/.openspec.yaml` 另加兩欄：`remote_version`（最後同步的服務端
版本）與 `remote_digest`（最後同步時的內容雜湊，`content_digest`）。兩者都不進服務端
文件，`content_digest` 也排除它們；本機內容雜湊 ≠ `remote_digest` 即「本機有未推送的
修改」（pull／edit 不覆寫；doctor `tasks.version_sync_agreement` 用同一套比對）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import specs
from .workspace import (
    META_FILE,
    NAME_RE,
    SPACE_DEV,
    Change,
    Workspace,
    atomic_write_text,
    write_yaml,
)

SCHEMA = 1
MIME = "application/json"
CHANGE_PREFIX = "task-change:"
INDEX_KEY = "task-index"
MIRROR_PREFIX = "task-spec-mirror:"
AUTHORIZATION_PREFIX = "task-authorization:"

STATE_ACTIVE = "active"
STATE_PENDING_APPLY = "pending_apply"
STATE_ARCHIVED = "archived"
STATES = (STATE_ACTIVE, STATE_PENDING_APPLY, STATE_ARCHIVED)

# 本機 `.openspec.yaml` 的同步欄位（不進服務端文件）
REMOTE_VERSION_KEY = "remote_version"
REMOTE_DIGEST_KEY = "remote_digest"
SYNC_FIELDS = (REMOTE_VERSION_KEY, REMOTE_DIGEST_KEY)

MIRROR_SOURCE_STDIO = "stdio"
# archive 段一推進鏡像的 write-ahead（語意同本機的 spec_applying／spec_applied_caps）
MIRROR_APPLYING_KEY = "mirror_applying"
MIRROR_APPLIED_KEY = "mirror_applied_caps"
PRINCIPAL_UI = "ui_session"
INDEX_RETRIES = 5

# change 目錄內的檔案 ↔ 文件欄位
FILE_FIELDS = {
    "proposal.md": "proposal_md",
    "design.md": "design_md",
    "tasks.md": "tasks_md",
}

Post = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


# ── 錯誤 ────────────────────────────────────────────────────────────


class RemoteError(Exception):
    """服務明確拒絕（4xx／5xx）。`code` 為服務錯誤碼（如 `version_conflict`）。"""

    def __init__(
        self,
        status: int | None,
        message: str,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body

    @property
    def code(self) -> str | None:
        error = (self.body or {}).get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) else None


class RemoteUnreachable(Exception):
    """服務不可達（連線、逾時、閘道錯誤）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class StoreError(Exception):
    """任務層語意的錯誤（名稱、狀態、格式）。`code` 給工具層轉成錯誤碼。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class VersionConflict(StoreError):
    """change 的 `expected_version` 過期；`current` 為服務端目前的 change
    （可能 None）。"""

    def __init__(self, name: str, expected: int, current: RemoteChange | None) -> None:
        actual = "不存在" if current is None else f"v{current.version}"
        super().__init__(
            "version_conflict",
            f"change {name} 版本衝突：預期 v{expected}，目前 {actual}",
        )
        self.expected = expected
        self.current = current


# ── 編碼 ────────────────────────────────────────────────────────────


def encode(data: Mapping[str, Any]) -> bytes:
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _decode(content: bytes, key: str) -> dict[str, Any]:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreError("invalid_remote_content", f"{key} 不是 UTF-8 JSON") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise StoreError("invalid_remote_content", f"{key} 的 schema 不是 {SCHEMA}")
    return data


def change_key(name: str) -> str:
    return f"{CHANGE_PREFIX}{name}"


def mirror_key(capability: str) -> str:
    return f"{MIRROR_PREFIX}{capability}"


def authorization_key(name: str) -> str:
    return f"{AUTHORIZATION_PREFIX}{name}"


def check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or name == "archive":
        raise StoreError(
            "invalid_name",
            f"change 名稱 {name!r} 只能用小寫英數與 -（kebab-case），且不可為 archive",
        )
    return name


def check_capability(cap: object) -> str:
    if not isinstance(cap, str) or not NAME_RE.match(cap):
        raise StoreError(
            "invalid_request", f"capability 名稱 {cap!r} 只能用小寫英數與 -"
        )
    return cap


def content_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    """內容雜湊依據的欄位：meta（去掉同步欄位）＋四類文字。"""
    meta = {k: v for k, v in (doc.get("meta") or {}).items() if k not in SYNC_FIELDS}
    return {
        "meta": meta,
        "proposal_md": doc.get("proposal_md") or "",
        "design_md": doc.get("design_md"),
        "tasks_md": doc.get("tasks_md") or "",
        "deltas": dict(doc.get("deltas") or {}),
    }


def content_digest(doc: Mapping[str, Any]) -> str:
    """change 工作內容的 sha256（本機與服務端比對「內容是否一致」用）。"""
    return hashlib.sha256(encode(content_fields(doc))).hexdigest()


def new_doc(name: str, meta: dict[str, Any], proposal_md: str, tasks_md: str) -> dict:
    return {
        "schema": SCHEMA,
        "name": name,
        "state": STATE_ACTIVE,
        "meta": meta,
        "proposal_md": proposal_md,
        "design_md": None,
        "tasks_md": tasks_md,
        "deltas": {},
        "apply": None,
    }


# ── 服務端 change 物件（與本機 Change 同介面）─────────────────────


@dataclass
class RemoteChange(Change):
    """服務端版本化內容上的 change：`workspace` 的驗證／推導與 archive 的 note 寫入
    （`read_file`／`plans`／`tasks_progress`／`save`）直接吃這個物件，不碰檔案系統。

    `meta` 與 `doc["meta"]` 是同一個 dict；`save()` 預設只改記憶體，持久化由呼叫端以
    `RemoteStore.save_change` 做（帶 `expected_version`）。`saver` 設定時 `save()` 會
    呼叫它（archive 在 worker thread 逐則 write-ahead 用）。"""

    doc: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    vault: str = ""
    saver: Callable[[], None] | None = field(default=None, repr=False)

    @classmethod
    def from_doc(cls, doc: dict[str, Any], version: int, vault: str) -> RemoteChange:
        name = check_name(doc.get("name"))
        meta = doc.get("meta")
        if not isinstance(meta, dict):
            raise StoreError("invalid_remote_content", f"change {name} 缺少 meta")
        doc.setdefault("deltas", {})
        doc.setdefault("design_md", None)
        doc.setdefault("apply", None)
        archived = doc.get("state") == STATE_ARCHIVED
        return cls(
            name=name,
            path=PurePosixPath(change_key(name)),  # type: ignore[arg-type]
            archived=archived,
            meta=meta,
            doc=doc,
            version=version,
            vault=vault,
        )

    @property
    def state(self) -> str:
        return str(self.doc.get("state") or STATE_ACTIVE)

    @property
    def deltas(self) -> dict[str, str]:
        return dict(self.doc.get("deltas") or {})

    def read_file(self, filename: str) -> str:
        fld = FILE_FIELDS.get(filename)
        value = self.doc.get(fld) if fld else None
        return value if isinstance(value, str) else ""

    def delta_files(self) -> dict[str, Path]:
        return {
            cap: PurePosixPath(f"specs/{cap}/spec.md")  # type: ignore[misc]
            for cap in sorted(self.deltas)
        }

    def plans(self) -> dict[str, specs.DeltaPlan]:
        return {
            cap: specs.parse_delta(text) for cap, text in sorted(self.deltas.items())
        }

    def save(self) -> None:
        if self.saver is not None:
            self.saver()

    def to_doc(self) -> dict[str, Any]:
        self.doc["meta"] = self.meta
        return self.doc

    def digest(self) -> str:
        return content_digest(self.to_doc())


@dataclass
class RemoteWorkspace(Workspace):
    """驗證用的工作區：change 來自服務端，主 spec 讀鏡像（§1.4：`read_main_spec`
    換成讀鏡像側載，`workspace` 的函式簽章不變）。資料由呼叫端先非同步載入。"""

    changes: list[RemoteChange] = field(default_factory=list)
    mirrors: dict[str, Mirror] = field(default_factory=dict)
    archived_names: set[str] = field(default_factory=set)
    decisions_map: dict[str, bool] | None = None

    def read_main_spec(self, capability: str) -> str | None:
        mirror = self.mirrors.get(capability)
        if mirror is None:
            raise StoreError(
                "mirror_missing", f"服務端沒有 capability {capability} 的主 spec 鏡像"
            )
        return mirror.text if mirror.exists else None

    def active(self) -> list[Change]:
        return [c for c in self.changes if c.state == STATE_ACTIVE]

    def archived(self) -> list[Change]:
        return [
            Change(n, PurePosixPath(n), True, {})  # type: ignore[arg-type]
            for n in sorted(self.archived_names)
        ]

    def find_active(self, name: str) -> Change | None:
        for change in self.changes:
            if change.name == name and change.state == STATE_ACTIVE:
                return change
        return None

    def decisions(self) -> dict[str, bool] | None:
        return self.decisions_map

    def missing_mirrors(self, change: RemoteChange) -> list[str]:
        return [cap for cap in sorted(change.deltas) if cap not in self.mirrors]


# ── 鏡像與授權紀錄 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Mirror:
    capability: str
    exists: bool
    text: str | None
    source: str
    version: int

    def same_content(self, exists: bool, text: str | None) -> bool:
        return self.exists == exists and (self.text if self.exists else None) == (
            text if exists else None
        )


@dataclass(frozen=True)
class AuthorizationRecord:
    vault: str
    change: str
    change_version: int
    authorized_by: str
    authorized_at: str
    principal_kind: str
    principal_name: str

    def to_meta(self) -> dict[str, Any]:
        """archive 開始時抄進 change meta 的 `authorization`。"""
        return {
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "change_version": self.change_version,
        }


def parse_authorization(data: Mapping[str, Any], name: str) -> AuthorizationRecord:
    """驗證授權紀錄格式；不合格拋 `StoreError("authorization_invalid")`。"""
    principal = data.get("principal")
    by = data.get("authorized_by")
    version = data.get("change_version")
    problems = []
    if data.get("change") != name:
        problems.append("change 名稱不符")
    if not (isinstance(by, str) and by.strip()):
        problems.append("缺少 authorized_by")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        problems.append("change_version 必須是正整數")
    if not isinstance(data.get("authorized_at"), str):
        problems.append("缺少 authorized_at")
    if not (isinstance(principal, dict) and principal.get("kind") == PRINCIPAL_UI):
        problems.append(f"principal.kind 必須是 {PRINCIPAL_UI}（UI session 核准）")
    if problems:
        raise StoreError(
            "authorization_invalid", f"{name} 的授權紀錄無效：" + "；".join(problems)
        )
    assert isinstance(principal, dict) and isinstance(by, str)
    return AuthorizationRecord(
        vault=str(data.get("vault") or ""),
        change=name,
        change_version=int(version),  # type: ignore[arg-type]
        authorized_by=by.strip(),
        authorized_at=str(data["authorized_at"]),
        principal_kind=PRINCIPAL_UI,
        principal_name=str(principal.get("name") or ""),
    )


# ── 存取 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Blob:
    content: bytes
    version: int
    mime: str | None


class RemoteStore:
    """單一 vault 的任務層服務端內容（space 固定 dev）。"""

    def __init__(self, post: Post, vault: str) -> None:
        self._post = post
        self.vault = vault

    async def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._post(path, {**body, "space": SPACE_DEV})

    # 側載

    async def get_blob(self, key: str) -> Blob | None:
        try:
            data = await self._call("/v1/blob_get", {"vault": self.vault, "key": key})
        except RemoteError as exc:
            if exc.status == 404 and exc.code == "not_found":
                return None
            raise
        return _blob(data)

    async def put_blob(
        self, key: str, content: bytes, *, expected_version: int | None
    ) -> int:
        """寫入並回傳新版本。`task-authorization:` 前綴一律拒絕
        （只能由 UI 核准寫入）。"""
        if key.startswith(AUTHORIZATION_PREFIX):
            raise StoreError(
                "authorization_write_forbidden",
                "任務層工具不可寫入授權紀錄（只能由 UI 核准）",
            )
        body: dict[str, Any] = {
            "vault": self.vault,
            "key": key,
            "mime": MIME,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        if expected_version is not None:
            body["expected_version"] = expected_version
        data = await self._call("/v1/blob_put", body)
        return int(data["version"])

    # change

    async def get_change(self, name: str) -> RemoteChange | None:
        check_name(name)
        blob = await self.get_blob(change_key(name))
        if blob is None:
            return None
        return RemoteChange.from_doc(
            _decode(blob.content, change_key(name)), blob.version, self.vault
        )

    async def require_change(self, name: str) -> RemoteChange:
        change = await self.get_change(name)
        if change is None:
            raise StoreError(
                "change_not_found", f"服務端沒有 change {name}（vault {self.vault}）"
            )
        return change

    async def create_change(self, doc: dict[str, Any]) -> RemoteChange:
        """以 `expected_version=0` 建立（名稱已存在 → `change_exists`），再補進索引。"""
        name = check_name(doc.get("name"))
        try:
            version = await self.put_blob(
                change_key(name), encode(doc), expected_version=0
            )
        except RemoteError as exc:
            if exc.code != "version_conflict":
                raise
            await self._ensure_indexed(name)
            raise StoreError(
                "change_exists", f"change {name} 已存在（vault {self.vault}）"
            ) from None
        await self.set_index_state(name, str(doc.get("state") or STATE_ACTIVE))
        return RemoteChange.from_doc(doc, version, self.vault)

    async def save_change(self, change: RemoteChange) -> int:
        """以 `change.version` 為 `expected_version` 寫回；
        成功時更新 `change.version`。"""
        try:
            version = await self.put_blob(
                change_key(change.name),
                encode(change.to_doc()),
                expected_version=change.version,
            )
        except RemoteError as exc:
            if exc.code != "version_conflict":
                raise
            raise VersionConflict(
                change.name, change.version, self._conflict_current(exc, change.name)
            ) from None
        change.version = version
        return version

    def _conflict_current(self, exc: RemoteError, name: str) -> RemoteChange | None:
        error = (exc.body or {}).get("error") or {}
        current = error.get("current") if isinstance(error, dict) else None
        if not isinstance(current, dict):
            return None
        try:
            blob = _blob(current)
            return RemoteChange.from_doc(
                _decode(blob.content, change_key(name)), blob.version, self.vault
            )
        except (StoreError, KeyError, ValueError):
            return None

    # 索引

    async def get_index(self) -> tuple[dict[str, Any], int]:
        blob = await self.get_blob(INDEX_KEY)
        if blob is None:
            return {"schema": SCHEMA, "changes": {}}, 0
        return _decode(blob.content, INDEX_KEY), blob.version

    async def ensure_index(self) -> bool:
        """索引不存在時建立空索引；回傳是否新建。"""
        try:
            await self.put_blob(
                INDEX_KEY,
                encode({"schema": SCHEMA, "changes": {}}),
                expected_version=0,
            )
        except RemoteError as exc:
            if exc.code == "version_conflict":
                return False
            raise
        return True

    async def set_index_state(self, name: str, state: str) -> None:
        """CAS 更新索引中一個 change 的狀態；衝突時重讀重試。"""
        for _ in range(INDEX_RETRIES):
            index, version = await self.get_index()
            changes = dict(index.get("changes") or {})
            if (changes.get(name) or {}).get("state") == state:
                return
            changes[name] = {"state": state}
            index["changes"] = dict(sorted(changes.items()))
            try:
                await self.put_blob(INDEX_KEY, encode(index), expected_version=version)
                return
            except RemoteError as exc:
                if exc.code != "version_conflict":
                    raise
        raise StoreError(
            "index_conflict", f"索引更新連續衝突 {INDEX_RETRIES} 次，請稍後重試"
        )

    async def _ensure_indexed(self, name: str) -> None:
        index, _ = await self.get_index()
        if name not in (index.get("changes") or {}):
            change = await self.get_change(name)
            if change is not None:
                await self.set_index_state(name, change.state)

    async def list_changes(self) -> tuple[list[RemoteChange], set[str]]:
        """(索引中 active／pending_apply 的 change，已封存名稱)；
        狀態以 change 文件為準。

        已封存名稱＝索引標 `archived` 或 change 文件為 `pending_apply` 者。"""
        index, _ = await self.get_index()
        changes: list[RemoteChange] = []
        archived: set[str] = set()
        for name, entry in sorted((index.get("changes") or {}).items()):
            if (entry or {}).get("state") == STATE_ARCHIVED:
                archived.add(name)
                continue
            change = await self.get_change(name)
            if change is None:
                continue
            if change.state == STATE_ARCHIVED:
                archived.add(name)
                continue
            if change.state == STATE_PENDING_APPLY:
                archived.add(name)
            changes.append(change)
        return changes, archived

    # 鏡像

    async def get_mirror(self, capability: str) -> Mirror | None:
        blob = await self.get_blob(mirror_key(capability))
        if blob is None:
            return None
        data = _decode(blob.content, mirror_key(capability))
        exists = bool(data.get("exists"))
        text = data.get("text") if exists else None
        if exists and not isinstance(text, str):
            raise StoreError(
                "invalid_remote_content", f"{mirror_key(capability)} 缺少 text"
            )
        return Mirror(
            capability, exists, text, str(data.get("source") or ""), blob.version
        )

    async def put_mirror(
        self,
        capability: str,
        *,
        exists: bool,
        text: str | None,
        source: str,
        expected_version: int | None,
    ) -> int:
        check_capability(capability)
        doc = {
            "schema": SCHEMA,
            "capability": capability,
            "exists": exists,
            "text": text if exists else None,
            "source": source,
        }
        return await self.put_blob(
            mirror_key(capability), encode(doc), expected_version=expected_version
        )

    # 授權紀錄（唯讀）

    async def get_authorization(self, name: str) -> AuthorizationRecord | None:
        blob = await self.get_blob(authorization_key(name))
        if blob is None:
            return None
        try:
            data = json.loads(blob.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StoreError(
                "authorization_invalid", f"{name} 的授權紀錄不是 JSON"
            ) from None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise StoreError(
                "authorization_invalid", f"{name} 的授權紀錄 schema 不是 {SCHEMA}"
            )
        return parse_authorization(data, name)

    # note（archive 段一）

    async def resolve_vault(self, key: str) -> str:
        data = await self._call("/v1/vault_resolve", {"key": key})
        return str(data["key"])

    async def list_topic(self, topic: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "vault": self.vault,
                "topics": [topic],
                "kinds": ["note"],
                "limit": 50,
            }
            if cursor:
                body["cursor"] = cursor
            data = await self._call("/v1/list", body)
            items.extend(
                i for i in data.get("items", []) if topic in (i.get("topics") or [])
            )
            cursor = data.get("next_cursor")
            if not cursor:
                return items

    async def write_note(self, body: dict[str, Any]) -> str:
        data = await self._call("/v1/write", {**body, "vault": self.vault})
        return str(data["id"])


def _blob(data: Mapping[str, Any]) -> Blob:
    try:
        content = base64.b64decode(str(data["content_base64"]), validate=True)
    except (binascii.Error, ValueError):
        raise StoreError("invalid_remote_content", "側載內容不是合法 base64") from None
    return Blob(content, int(data.get("version") or 0), data.get("mime"))


async def all_indexes(post: Post) -> dict[str, dict[str, Any]]:
    """`vault='*'`：本 space 內每個 vault 的索引 `{vault: index}`。"""
    data = await post("/v1/blob_get", {"key": INDEX_KEY, "space": SPACE_DEV})
    result = {}
    for item in data.get("items") or []:
        try:
            result[str(item["vault"])] = _decode(_blob(item).content, INDEX_KEY)
        except (StoreError, KeyError):
            continue
    return result


class ThreadBridgeClient:
    """給 worker thread 裡跑的同步 archive note 寫入（`archive.write_notes`）用：
    介面同 `VaultClient.list_topic`／`write`，實際經 `anyio.from_thread.run` 回到事件
    迴圈呼叫 `RemoteStore`（MCP 走 `Shell._send`，HTTP 模式是同迴圈 loopback，
    不可直接用同步 client）。錯誤轉成 `hooks.service` 的例外，與 CLI 路徑一致。"""

    def __init__(self, store: RemoteStore) -> None:
        self.store = store

    def _run(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        import anyio.from_thread

        from .vault_client import ServiceRejected, ServiceUnavailable

        try:
            return anyio.from_thread.run(fn, *args)
        except RemoteUnreachable as exc:
            raise ServiceUnavailable(exc.detail) from None
        except RemoteError as exc:
            raise ServiceRejected(exc.message, exc.status, exc.body) from None

    def list_topic(self, vault: str, space: str, topic: str) -> list[dict[str, Any]]:
        return self._run(self.store.list_topic, topic)

    def write(
        self,
        vault: str,
        space: str,
        *,
        title: str,
        body: str,
        topics: Any,
        links: Any = (),
        supersedes: str | None = None,
        author: str | None = None,
    ) -> str:
        return self._run(
            self.store.write_note,
            {
                "title": title,
                "body": body,
                "topics": list(topics),
                "links": list(links),
                "supersedes": supersedes,
                "author": author,
            },
        )


def vault_client_post(client: Any) -> Post:
    """把同步的 `tasks.vault_client.VaultClient` 包成 `Post`（給 MCP-T6 的 CLI 改接）。

    CLI 沒有事件迴圈，以 `asyncio.run(...)` 呼叫 `RemoteStore` 的方法即可；錯誤轉成
    `RemoteError`／`RemoteUnreachable`。"""
    from .vault_client import ServiceRejected, ServiceUnavailable

    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return client._post(path, body)
        except ServiceUnavailable as exc:
            raise RemoteUnreachable(exc.detail) from None
        except ServiceRejected as exc:
            payload = exc.body if isinstance(exc.body, dict) else None
            raise RemoteError(exc.status, exc.detail, payload) from None

    return post


# ── 本機工作副本 ───────────────────────────────────────────────────


def _read_raw(path: Path) -> str | None:
    """原樣讀（`newline=""` 保留 CRLF）；不存在回 None。"""
    if not path.is_file():
        return None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return fh.read()


def local_doc(change: Change) -> dict[str, Any]:
    """本機 change 目錄 → 服務端文件格式（meta 去掉同步欄位）。給遷移（MCP-T7）與
    CLI 推送（MCP-T6）用，也是本機「內容雜湊」的依據。"""
    meta = {k: v for k, v in change.meta.items() if k not in SYNC_FIELDS}
    deltas = {}
    for cap, path in change.delta_files().items():
        text = _read_raw(path)
        if text is not None:
            deltas[cap] = text
    return {
        "schema": SCHEMA,
        "name": change.name,
        "state": STATE_ACTIVE,
        "meta": meta,
        "proposal_md": _read_raw(change.path / "proposal.md") or "",
        "design_md": _read_raw(change.path / "design.md"),
        "tasks_md": _read_raw(change.path / "tasks.md") or "",
        "deltas": deltas,
        "apply": None,
    }


@dataclass(frozen=True)
class LocalState:
    """本機工作副本相對服務端的狀態。

    `state`：`absent`（本機沒有）、`in_sync`、`behind`（本機未改、服務端較新）、
    `local_modified`（本機有未推送的修改）、`diverged`（本機改過且服務端也較新）、
    `unknown`（本機 `.openspec.yaml` 無法解析）。"""

    state: str
    local_version: int | None = None

    @property
    def safe_to_overwrite(self) -> bool:
        return self.state in ("absent", "in_sync", "behind")


def local_state(ws: Workspace, remote: RemoteChange) -> LocalState:
    local = ws.find_active(remote.name)
    if local is None:
        return LocalState("absent")
    if local.meta_error:
        return LocalState("unknown")
    raw_version = local.meta.get(REMOTE_VERSION_KEY)
    version = raw_version if isinstance(raw_version, int) else None
    recorded = local.meta.get(REMOTE_DIGEST_KEY)
    current = content_digest(local_doc(local))
    if current == remote.digest():
        return LocalState("in_sync", version)
    modified = current != recorded
    if not modified:
        return LocalState("behind", version)
    if version is not None and version < remote.version and recorded is not None:
        return LocalState("diverged", version)
    return LocalState("local_modified", version)


def write_local(ws: Workspace, remote: RemoteChange) -> Path:
    """把服務端內容寫成本機工作副本（覆寫；多出的 delta 刪除），記同步欄位。"""
    path = ws.changes_dir / remote.name
    doc = remote.to_doc()
    for filename, fld in FILE_FIELDS.items():
        value = doc.get(fld)
        target = path / filename
        if isinstance(value, str):
            atomic_write_text(target, value)
        elif target.is_file():
            target.unlink()
    wanted = dict(doc.get("deltas") or {})
    specs_dir = path / "specs"
    if specs_dir.is_dir():
        for existing in specs_dir.glob("*/spec.md"):
            if existing.parent.name not in wanted:
                existing.unlink()
                if not any(existing.parent.iterdir()):
                    existing.parent.rmdir()
    for cap, text in wanted.items():
        atomic_write_text(specs_dir / check_capability(cap) / "spec.md", text)
    meta = dict(doc.get("meta") or {})
    meta[REMOTE_VERSION_KEY] = remote.version
    meta[REMOTE_DIGEST_KEY] = remote.digest()
    write_yaml(path / META_FILE, meta)
    return path


def local_main_specs(ws: Workspace) -> dict[str, str]:
    """本機 `specs/<cap>/spec.md` 的原樣內容（鏡像推送用）。"""
    result = {}
    if ws.specs_dir.is_dir():
        for path in sorted(ws.specs_dir.glob("*/spec.md")):
            cap = path.parent.name
            if NAME_RE.match(cap):
                text = _read_raw(path)
                if text is not None:
                    result[cap] = text
    return result
