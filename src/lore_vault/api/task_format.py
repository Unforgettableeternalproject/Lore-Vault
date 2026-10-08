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
