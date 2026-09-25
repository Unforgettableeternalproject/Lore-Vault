"""spike 接入端點（階段 8）：episode 收料、主機管線讀寫 concept、注入 side-car。

- `POST /v1/episodes`：客戶端 spool 推送；逐筆結果，冪等；vault 不存在時自動建立
  （只限這條路徑，notes write 仍不自動建）
- `GET /v1/episodes`：主機管線分頁讀取（vault 必填，跨 vault 明示 `"*"`）
- `GET /v1/concepts/export`：與 spike `concepts.json` 同格式（PreToolUse scorer 的快照）
- `POST /v1/concepts`：主機管線批次 upsert／刪除（整批成功或整批不寫）
- `POST /v1/injections`：注入 side-car 批次記錄，冪等

每一批在單一交易內處理；逐筆結果的端點以 SAVEPOINT 隔離單筆失敗，
一筆失敗不影響同批其他筆，也不會留下半套寫入（例如自動建了 vault 卻沒寫進 episode）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lore_vault.notes import InvalidCursor
from lore_vault.schema import (
    MISSING,
    Concept,
    Episode,
    Injection,
    SchemaError,
    Vault,
    canonical_key,
)
from lore_vault.storage import records
from lore_vault.storage import snapshot as storage_snapshot
from lore_vault.storage.db import transaction
from lore_vault.storage.errors import (
    DuplicateRecord,
    NotFound,
    UnknownVault,
    VaultConflict,
    VaultRequired,
)
from lore_vault.storage.timeutil import utc_now
from lore_vault.storage.vaults import (
    ALL_VAULTS,
    ORIGIN_EPISODE,
    ORIGIN_PIPELINE,
    ensure_vault,
    get_vault,
    resolve_write,
)

from .errors import error_body
from .state import AppState

router = APIRouter(prefix="/v1")

EPISODE_BATCH_MAX = 200
EPISODE_PAGE_DEFAULT = 200
EPISODE_PAGE_MAX = 1000
CONCEPT_BATCH_MAX = 1000
INJECTION_BATCH_MAX = 500

# 通用 concept（scope=None）的歸屬 vault；與 ON 匯入的 `[PM] Global` 同一個 key
GLOBAL_VAULT_KEY = "global"
GLOBAL_VAULT_DISPLAY = "Global"

HEADER_CONCEPT_COUNT = "X-Lore-Vault-Concepts"
HEADER_EXCLUDED_MISSING_SCOPE = "X-Lore-Vault-Excluded-Missing-Scope"


def _state(request: Request) -> AppState:
    return request.app.state.lore


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """單筆隔離：例外時退回到這筆開始前的狀態，外層交易繼續。"""
    conn.execute("SAVEPOINT item")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO item")
        conn.execute("RELEASE item")
        raise
    conn.execute("RELEASE item")


def _check_batch(name: str, items: list[Any], maximum: int) -> None:
    if len(items) > maximum:
        raise ValueError(f"{name} 每批最多 {maximum} 筆，收到 {len(items)} 筆")


def _split_vault(item: Any) -> tuple[dict[str, Any], Any]:
    """拆出 body 每筆附帶的 `vault`（schema 型別沒有這欄）；非物件直接拒絕。"""
    if not isinstance(item, dict):
        raise SchemaError(f"每筆必須是物件，得到 {type(item).__name__}")
    data = dict(item)
    return data, data.pop("vault", MISSING)


def _require_vault(value: Any) -> str:
    if value is MISSING or value is None:
        raise VaultRequired("每筆都必須帶 vault")
    if not isinstance(value, str):
        raise VaultRequired(f"vault 必須是字串，得到 {type(value).__name__}")
    return value


# ── episodes ────────────────────────────────────────────────────────


class EpisodeBatch(_Req):
    episodes: list[Any] = Field(
        description=f"Episode dict 另加 vault；每批最多 {EPISODE_BATCH_MAX}"
    )


def _episode_key(item: Any) -> list[Any] | None:
    if not isinstance(item, dict):
        return None
    return [item.get("session_id"), item.get("prompt_id"), item.get("turn_index")]


def _auto_vault_detail(episode: Episode) -> str:
    return json.dumps(
        {
            "machine": episode.machine,
            "repo": episode.repo,
            "session_id": episode.session_id,
            "created_at": utc_now(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@router.post("/episodes")
def post_episodes(request: Request, req: EpisodeBatch) -> dict[str, Any]:
    """逐筆結果：accepted／duplicate（同鍵同內容，視為成功）／conflict（同鍵不同內容，
    客戶端應保留在 spool 並回報）／invalid（schema 或 vault 不合法）。"""
    _check_batch("episodes", req.episodes, EPISODE_BATCH_MAX)
    results: list[dict[str, Any]] = []
    created_vaults: list[str] = []
    tally = {"accepted": 0, "duplicate": 0, "conflict": 0, "invalid": 0}
    with _state(request).connection() as conn, transaction(conn):
        for index, item in enumerate(req.episodes):
            result: dict[str, Any] = {"index": index, "key": _episode_key(item)}
            try:
                data, raw_vault = _split_vault(item)
                vault = _require_vault(raw_vault)
                episode = Episode.from_dict(data)
                with _savepoint(conn):
                    key, created = ensure_vault(
                        conn,
                        Vault(key=vault, display=episode.repo or vault, kind="repo"),
                        origin=ORIGIN_EPISODE,
                        origin_detail=_auto_vault_detail(episode),
                    )
                    inserted = records.insert_episode(conn, key, episode)
                result["vault"] = key
                result["status"] = "accepted" if inserted else "duplicate"
                if created:
                    created_vaults.append(key)
            except DuplicateRecord as exc:
                result.update(status="conflict", error=str(exc))
            except (SchemaError, VaultRequired, VaultConflict, ValueError) as exc:
                result.update(status="invalid", error=str(exc))
            tally[result["status"]] += 1
            results.append(result)
    return {
        "accepted": tally["accepted"],
        "duplicates": tally["duplicate"],
        "conflicts": tally["conflict"],
        "invalid": tally["invalid"],
        "created_vaults": created_vaults,
        "results": results,
    }


def _encode_episode_cursor(cursor: tuple[str, int]) -> str:
    raw = json.dumps(list(cursor), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_episode_cursor(cursor: str) -> tuple[str, int]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidCursor(f"cursor 無法解析：{cursor!r}") from exc
    if (
        not isinstance(data, list)
        or len(data) != 2
        or not isinstance(data[0], str)
        or isinstance(data[1], bool)
        or not isinstance(data[1], int)
    ):
        raise InvalidCursor(f"cursor 格式錯誤：{cursor!r}")
    return data[0], data[1]


@router.get("/episodes")
def get_episodes(
    request: Request,
    vault: str | None = Query(None, description="vault key 或別名；跨 vault 明示 '*'"),
    since: str | None = Query(None, description="started_at >= since（UTC）"),
    session_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(EPISODE_PAGE_DEFAULT, ge=1, le=EPISODE_PAGE_MAX),
) -> dict[str, Any]:
    """依 (started_at, seq) 由舊到新分頁；每筆是 Episode dict 另加凍結的 `vault`。"""
    decoded = _decode_episode_cursor(cursor) if cursor is not None else None
    with _state(request).connection() as conn:
        rows, next_cursor = records.list_episode_rows(
            conn,
            vault,  # type: ignore[arg-type]  # None → VaultRequired（400）
            session_id=session_id,
            since=since,
            limit=limit,
            cursor=decoded,
        )
    return {
        "items": [{**episode.to_dict(), "vault": key} for key, episode in rows],
        "next_cursor": _encode_episode_cursor(next_cursor) if next_cursor else None,
    }


# ── concepts ────────────────────────────────────────────────────────


def concept_to_spike(concept: Concept) -> dict[str, Any]:
    """Concept → spike `concepts.json` 的一筆。

    欄位順序與 spike `distill.ingest` 相同。`usability` 只有注入實驗寫過才出現
    （spike 從不寫 `usability: null`），所以 None 時省略，其餘欄位（含 None）照舊輸出。
    """
    data = concept.to_dict()
    if data.get("usability") is None:
        data.pop("usability", None)
    return data


def render_export(concepts: list[Concept]) -> bytes:
    """序列化方式固定（同 spike：`ensure_ascii=False, indent=2`），ETag 才穩定。"""
    return json.dumps(
        [concept_to_spike(c) for c in concepts], ensure_ascii=False, indent=2
    ).encode("utf-8")


@router.get("/concepts/export")
def export_concepts(
    request: Request,
    vault: str = Query(
        ALL_VAULTS,
        description="預設 '*'＝全部（scope 由客戶端 scorer 判斷）；可指定單一 vault",
    ),
) -> Response:
    """spike `concepts.json` 同格式（頂層 list）。

    ETag 為內容 sha256；If-None-Match 符合回 304。
    """
    with _state(request).connection() as conn:
        concepts, excluded = records.export_concepts(conn, vault)
    body = render_export(concepts)
    digest = hashlib.sha256(body).hexdigest()
    headers = {
        "ETag": storage_snapshot.etag(digest),
        HEADER_CONCEPT_COUNT: str(len(concepts)),
        HEADER_EXCLUDED_MISSING_SCOPE: str(excluded),
        "Cache-Control": "no-cache",
    }
    if storage_snapshot.etag_matches(request.headers.get("if-none-match"), digest):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


class ConceptBatch(_Req):
    vault: str | None = Field(
        None,
        description="單一 vault（操作限於此 vault）或 '*'（依每筆 vault／既有歸屬）",
    )
    mode: str = Field(
        "upsert", description="upsert／create（已存在即衝突）／update（不存在即失敗）"
    )
    concepts: list[Any] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)


class _Reject(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _ensure_global(conn: sqlite3.Connection, created: list[str]) -> str:
    key, was_created = ensure_vault(
        conn,
        Vault(key=GLOBAL_VAULT_KEY, display=GLOBAL_VAULT_DISPLAY, kind="global"),
        origin=ORIGIN_PIPELINE,
        origin_detail=json.dumps(
            {"reason": "scope=None concept", "created_at": utc_now()},
            ensure_ascii=False,
        ),
    )
    if get_vault(conn, key).kind != "global":
        raise _Reject("conflict", f"vault {key!r} 的 kind 不是 global")
    if was_created:
        created.append(key)
    return key


def _concept_target(
    conn: sqlite3.Connection,
    batch_vault: str | None,
    concept: Concept,
    item_vault: Any,
    created: list[str],
) -> str:
    """決定 concept 寫進哪個 vault。

    - 既有 id：沿用原歸屬（歸屬凍結，不因 scope 改變而搬家）；有帶 vault 必須一致
    - 新 id、scope=None：`global`（不存在時自動建 kind=global）；帶別的 vault 為 invalid
    - 新 id、scope 為 repo 名：用每筆的 vault，或批次指定的單一 vault
    """
    requested = None
    if item_vault is not MISSING:
        requested = resolve_write(conn, _require_vault(item_vault))
    if batch_vault is not None and requested is not None and requested != batch_vault:
        raise _Reject("invalid", f"每筆 vault {requested!r} 與批次 vault 不同")
    requested = requested or batch_vault
    owner = records.concept_owner(conn, concept.id)
    if owner is not None:
        if requested is not None and requested != owner:
            raise _Reject("conflict", f"concept {concept.id!r} 已屬於 vault {owner!r}")
        return owner
    if concept.scope is None:
        if requested is not None and requested != canonical_key(GLOBAL_VAULT_KEY):
            raise _Reject(
                "invalid",
                f"scope=None 的通用 concept 只能寫進 vault {GLOBAL_VAULT_KEY!r}",
            )
        return _ensure_global(conn, created)
    if requested is None:
        raise VaultRequired("新 concept 必須帶 vault（或批次指定單一 vault）")
    return requested


@router.post("/concepts")
def post_concepts(request: Request, req: ConceptBatch) -> Response:
    """整批成功或整批不寫：任一筆 invalid／conflict → 回 400／409 與逐筆結果，不寫入。

    `delete` 的 id 不存在回 `not_found`（收斂重跑的冪等，不算失敗）。
    """
    total = len(req.concepts) + len(req.delete)
    _check_batch("concepts + delete", [None] * total, CONCEPT_BATCH_MAX)
    if req.mode not in records.UPSERT_MODES:
        raise ValueError(
            f"mode 必須是 {sorted(records.UPSERT_MODES)}，得到 {req.mode!r}"
        )
    if req.vault is None:
        raise VaultRequired("必須指定 vault；跨 vault 請明示 vault='*'")

    results: list[dict[str, Any]] = []
    delete_results: list[dict[str, Any]] = []
    created_vaults: list[str] = []
    seen: set[str] = set()
    failed = {"invalid": 0, "conflict": 0}
    with _state(request).connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            batch_vault = (
                None if req.vault == ALL_VAULTS else resolve_write(conn, req.vault)
            )
            for index, item in enumerate(req.concepts):
                result: dict[str, Any] = {
                    "index": index,
                    "id": item.get("id") if isinstance(item, dict) else None,
                }
                try:
                    data, item_vault = _split_vault(item)
                    concept = Concept.from_dict(data)
                    if concept.scope is MISSING:
                        raise _Reject(
                            "invalid", "必須帶 scope（repo 名，或 null＝跨專案通用）"
                        )
                    if concept.id in seen:
                        raise _Reject("invalid", f"同一批重複的 id {concept.id!r}")
                    seen.add(concept.id)
                    with _savepoint(conn):
                        target = _concept_target(
                            conn, batch_vault, concept, item_vault, created_vaults
                        )
                        outcome = records.upsert_concept(
                            conn, target, concept, mode=req.mode
                        )
                    result.update(vault=target, status=outcome)
                except _Reject as exc:
                    result.update(status=exc.status, error=str(exc))
                except (DuplicateRecord, VaultConflict) as exc:
                    result.update(status="conflict", error=str(exc))
                except NotFound as exc:
                    result.update(status="invalid", error=str(exc))
                except UnknownVault as exc:
                    result.update(status="invalid", error=str(exc))
                except (SchemaError, VaultRequired, ValueError) as exc:
                    result.update(status="invalid", error=str(exc))
                if result["status"] in failed:
                    failed[result["status"]] += 1
                results.append(result)
            for concept_id in req.delete:
                entry: dict[str, Any] = {"id": concept_id}
                if concept_id in seen:
                    entry.update(status="invalid", error="同一批同時 upsert 與刪除")
                else:
                    seen.add(concept_id)
                    owner = records.concept_owner(conn, concept_id)
                    if owner is None:
                        entry["status"] = "not_found"
                    elif batch_vault is not None and owner != batch_vault:
                        # 限定單一 vault 的批次看不到別的 vault：不洩漏存在與否
                        entry["status"] = "not_found"
                    else:
                        records.delete_concept(conn, owner, concept_id)
                        entry.update(vault=owner, status="deleted")
                if entry["status"] in failed:
                    failed[entry["status"]] += 1
                delete_results.append(entry)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if failed["invalid"] or failed["conflict"]:
            conn.execute("ROLLBACK")
            code = 409 if failed["conflict"] else 400
            return JSONResponse(
                error_body(
                    "batch_rejected",
                    f"{failed['invalid']} 筆 invalid、"
                    f"{failed['conflict']} 筆 conflict；整批未寫入",
                    results=results,
                    delete_results=delete_results,
                ),
                status_code=code,
            )
        conn.execute("COMMIT")

    def count(items: list[dict[str, Any]], name: str) -> int:
        return sum(1 for r in items if r["status"] == name)

    return JSONResponse(
        {
            "applied": True,
            "created": count(results, records.CONCEPT_CREATED),
            "updated": count(results, records.CONCEPT_UPDATED),
            "unchanged": count(results, records.CONCEPT_UNCHANGED),
            "deleted": count(delete_results, "deleted"),
            "not_found": count(delete_results, "not_found"),
            "created_vaults": created_vaults,
            "results": results,
            "delete_results": delete_results,
        }
    )


# ── injections ──────────────────────────────────────────────────────


class InjectionBatch(_Req):
    injections: list[Any] = Field(
        description="Injection dict 另加 vault（必填）與選填 recorded（UTC）；"
        f"每批最多 {INJECTION_BATCH_MAX}"
    )


@router.post("/injections")
def post_injections(request: Request, req: InjectionBatch) -> dict[str, Any]:
    """逐筆結果：accepted／duplicate（內容相同的重送）／unknown_vault（vault 還不存在，
    不自動建；客戶端保留稍後重送）／invalid。"""
    _check_batch("injections", req.injections, INJECTION_BATCH_MAX)
    results: list[dict[str, Any]] = []
    tally = {"accepted": 0, "duplicate": 0, "unknown_vault": 0, "invalid": 0}
    with _state(request).connection() as conn, transaction(conn):
        for index, item in enumerate(req.injections):
            result: dict[str, Any] = {"index": index}
            try:
                data, raw_vault = _split_vault(item)
                vault = _require_vault(raw_vault)
                recorded = data.pop("recorded", None)
                injection = Injection.from_dict(data)
                with _savepoint(conn):
                    inserted = records.insert_injection(
                        conn, vault, injection, recorded=recorded
                    )
                result["status"] = "accepted" if inserted else "duplicate"
            except UnknownVault as exc:
                result.update(status="unknown_vault", error=str(exc))
            except (SchemaError, VaultRequired, ValueError) as exc:
                result.update(status="invalid", error=str(exc))
            tally[result["status"]] += 1
            results.append(result)
    return {
        "accepted": tally["accepted"],
        "duplicates": tally["duplicate"],
        "unknown_vault": tally["unknown_vault"],
        "invalid": tally["invalid"],
        "results": results,
    }
