"""任務層服務端資料格式中「核心也要懂」的部分（只用標準庫）。

核心不 import 任務層（TASK_LAYER_MCP §2），但 `/v1/blob_put` 的寫入守衛與
`/v1/tasks_authorize` 必須在服務端強制授權閘門（bearer 由所有 agent 共用，客戶端工具的
參數限制擋不住直接打 HTTP 的呼叫者），所以下列定義放在核心，任務層
`lore_vault.tasks.remote_store` 直接 import 這裡（依賴方向：任務層 → 核心），
兩邊只有一份定義，不會漂移。

授權內容雜湊（`authorization_digest`）：UI 核准綁定的「change 內容」。範圍是
`name`、`proposal_md`、`design_md`、`tasks_md`、`deltas`，以及 meta 去掉
`BOOKKEEPING_META_KEYS` 後的欄位；頂層 `state`、`apply` 不算。archive 段一／段二的
簿記寫入（note write-ahead、鏡像推進、落地進度）因此不會讓核准失效，任何實質內容的
修改（含 `requires_authorization`、`base`、delta）都會。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SCHEMA = 1
CHANGE_PREFIX = "task-change:"
AUTHORIZATION_PREFIX = "task-authorization:"
# vault 內 change 的列舉；存在且未停用＝該 vault 已啟用任務層（`/v1/tasks_enable`、
# `tasks init`）。停用（`/v1/tasks_disable`、`tasks disable`）只在索引加 `disabled`
# 欄位 `{"at": 時間, "by": 誰}`，不刪任何內容；重新啟用移除該欄位即復原
INDEX_KEY = "task-index"
DISABLED_KEY = "disabled"
# 任務層 UI 快照（任務層 `snapshot` 推送；核心只為停用守衛認得這個 key）
SNAPSHOT_KEY = "tasks-snapshot"

STATE_ACTIVE = "active"
STATE_PENDING_APPLY = "pending_apply"
STATE_ARCHIVED = "archived"
STATES = (STATE_ACTIVE, STATE_PENDING_APPLY, STATE_ARCHIVED)

PRINCIPAL_UI = "ui_session"
REQUIRES_AUTHORIZATION_KEY = "requires_authorization"
# 試用期封存、沒有 note 的 change 以此欄位註明（遷移的舊封存可能帶）
LEGACY_ARCHIVE_KEY = "legacy_archive"

# 本機 `.openspec.yaml` 的同步欄位（不進服務端文件）
SYNC_META_KEYS = ("remote_version", "remote_digest")
# archive 簿記：段一（服務端）與本機 archive 寫進 meta 的欄位，不算 change 內容
BOOKKEEPING_META_KEYS = frozenset(
    {
        "vault",
        "notes",
        "note_digests",
        "note_id",
        "authorized_by",
        "authorization",
        "incomplete_at_archive",
        "archive_reason",
        "archived_at",
        "mirror_applying",
        "mirror_applied_caps",
        "spec_applying",
        "spec_applied_caps",
        "spec_applied",
        *SYNC_META_KEYS,
    }
)


def encode(data: Mapping[str, Any]) -> bytes:
    """服務端 JSON 的固定編碼（UTF-8、鍵排序、無空白）。"""
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def empty_index() -> dict[str, Any]:
    """新建的空索引（`/v1/tasks_enable` 與任務層 `ensure_index` 共用）。"""
    return {"schema": SCHEMA, "changes": {}}


def is_disabled(index: Mapping[str, Any]) -> bool:
    """索引是否標記停用（欄位存在且不是 null／false；格式不對也當成停用，偏向拒絕）。"""
    return index.get(DISABLED_KEY) not in (None, False)


def disabled_info(index: Mapping[str, Any]) -> dict[str, Any] | None:
    """停用資訊 `{"at", "by"}`（值不是字串時為 null）；未停用回 None。"""
    if not is_disabled(index):
        return None
    raw = index.get(DISABLED_KEY)
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        k: raw.get(k) if isinstance(raw.get(k), str) else None for k in ("at", "by")
    }


def valid_disabled(value: object) -> bool:
    """寫入索引的 `disabled` 欄位格式：`{"at": 非空字串, "by": 非空字串}`。"""
    return (
        isinstance(value, Mapping)
        and set(value) == {"at", "by"}
        and all(isinstance(value[k], str) and value[k].strip() for k in ("at", "by"))
    )


def guarded_by_disable(key: str) -> bool:
    """停用中拒絕寫入的 key：`task-` 開頭（索引本身另有規則）與 UI 快照。"""
    return (key.startswith("task-") and key != INDEX_KEY) or key == SNAPSHOT_KEY


def _meta(doc: Mapping[str, Any]) -> dict[str, Any]:
    meta = doc.get("meta")
    return dict(meta) if isinstance(meta, Mapping) else {}


def authorization_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    """核准綁定的內容（見模組 docstring）。"""
    return {
        "name": doc.get("name"),
        "meta": {k: v for k, v in _meta(doc).items() if k not in BOOKKEEPING_META_KEYS},
        "proposal_md": doc.get("proposal_md") or "",
        "design_md": doc.get("design_md"),
        "tasks_md": doc.get("tasks_md") or "",
        "deltas": dict(doc.get("deltas") or {}),
    }


def authorization_digest(doc: Mapping[str, Any]) -> str:
    """change 內容雜湊（sha256 hex），授權紀錄的 `content_digest` 與它比對。"""
    return hashlib.sha256(encode(authorization_fields(doc))).hexdigest()


def bookkeeping(doc: Mapping[str, Any]) -> dict[str, Any]:
    """archive 簿記的投影（meta 的簿記欄位，不含本機同步欄位＋`apply`）。"""
    meta = _meta(doc)
    return {
        "meta": {
            k: meta[k]
            for k in sorted(BOOKKEEPING_META_KEYS - set(SYNC_META_KEYS))
            if k in meta
        },
        "apply": doc.get("apply"),
    }


def requires_authorization(doc: Mapping[str, Any]) -> bool:
    return _meta(doc).get(REQUIRES_AUTHORIZATION_KEY) is True


def is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )
