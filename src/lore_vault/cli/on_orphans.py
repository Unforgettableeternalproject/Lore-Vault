"""Open Notebook 孤兒 note（不屬任何 notebook）的歸屬與取回。

ON 1.14.0 有 note 沒掛在任何 notebook 上；全量 `GET /api/notes` 因某則 note 含 NUL
回 500，孤兒無法經 REST 列舉，清單要從外部取得（SurrealDB 唯讀查詢的 id／標題／時間）。

- `build_orphan_map`：到 Claude Code transcript（`~/.claude/projects/**/*.jsonl`）
  與 Codex rollout（`~/.codex/sessions/**/*.jsonl`）找建立該 note 的 `create_note`
  呼叫（標題比對，必要時以時間輔助），取該筆訊息的 `cwd`，以 `lore_vault.binding`
  算 vault key，再對 notebook mapping 的 vault 與別名得到最終 vault。
  另以 `update_note` 的 `note_id` 精確比對當輔助證據。
  **只讀工具呼叫的 input（title／note_id／notebook_id）與訊息的 cwd／timestamp**，
  不讀 tool_result、不輸出正文。找不到或歧義的標 `needs_review`，由人工填 `vault`。
- `export_orphans`：依孤兒 mapping 逐筆 `GET /api/notes/{id}` 取內容，寫進匯出目錄的
  `orphans.jsonl`（另有 `orphans-manifest.json` 記雜湊）。REST 取不到的（例如含
  NUL 的那則會 500）列為 unavailable，可用 `--supplement` 補上從 SurrealDB 唯讀副本
  手動匯出的紀錄（程序見 docs/DEVELOPMENT.md）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lore_vault.binding import lookup_key, resolve_binding
from lore_vault.schema import canonical_key

ORPHAN_MAP_FORMAT = 1
ORPHANS_FILE = "orphans.jsonl"
ORPHANS_MANIFEST_FILE = "orphans-manifest.json"

# 同標題建立多次且指向不同 vault 時，只採信與 ON created 相差這麼多以內的那幾筆
MATCH_WINDOW = timedelta(minutes=15)

BASIS_CREATE_TITLE = "create_note_title"
BASIS_CREATE_TITLE_TIME = "create_note_title_time"
BASIS_UPDATE_ID = "update_note_id"
BASIS_EXCLUDED = "excluded"
BASIS_MANUAL = "manual"

# Claude Code 工具名用連字號、Codex 的 namespace 用底線
_LINE_HINTS = (b"open-notebook__", b"mcp__open_notebook__")


class OrphanError(Exception):
    """孤兒 mapping 或取回無法繼續。訊息不含 note 內容。"""


def _norm_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    # ON 的時間有奈秒位數，fromisoformat 只吃到微秒
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tail = rest[len(digits) :]
        text = f"{head}.{digits[:6]}{tail}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ── 孤兒清單輸入 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrphanInput:
    id: str
    title: str
    created: str | None


def load_orphan_list(path: Path) -> list[OrphanInput]:
    """孤兒清單：JSON 陣列／JSONL（物件含 id、title、created），或 TSV
    （第 1 欄 id、第 2 欄 created、最後一欄 title；調查時的輸出格式）。"""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    rows: list[OrphanInput] = []
    if stripped.startswith("["):
        items = json.loads(stripped)
    elif stripped.startswith("{"):
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            cols = line.rstrip("\r\n").split("\t")
            if len(cols) < 3:
                raise OrphanError(f"孤兒清單 TSV 欄數不足：{cols[0]!r}")
            items.append({"id": cols[0], "created": cols[1], "title": cols[-1]})
    seen: set[str] = set()
    for item in items:
        note_id = str(item.get("id") or "").strip()
        title = item.get("title")
        if not note_id or not isinstance(title, str):
            raise OrphanError(f"孤兒清單項缺 id 或 title：{note_id!r}")
        if note_id in seen:
            raise OrphanError(f"孤兒清單 id 重複：{note_id!r}")
        seen.add(note_id)
        created = item.get("created")
        rows.append(OrphanInput(note_id, title, created if created else None))
    return rows


# ── transcript 掃描 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolUseHit:
    tool: str  # "create_note"／"update_note"
    key: str  # create：正規化標題；update：note_id
    cwd: str | None
    timestamp: str | None
    transcript: str
    notebook_id: str | None = None


def _file_cwd(path: Path) -> str | None:
    """訊息本身沒帶 cwd 時，退回同一個 transcript 第一個帶 cwd 的訊息。"""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if b'"cwd"' not in raw:
                    continue
                try:
                    cwd = json.loads(raw).get("cwd")
                except ValueError:
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def iter_transcripts(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.jsonl"))


def _tool_kind(full_name: str) -> str | None:
    """ON MCP 工具名 → "create_note"／"update_note"。

    Claude Code 是 `mcp__open-notebook__create_note`；Codex 拆成 namespace
    `mcp__open_notebook__` + name `create_note`，連字號寫成底線。
    """
    name = full_name.lower().replace("-", "_")
    for kind in ("create_note", "update_note"):
        if name.endswith(f"open_notebook__{kind}"):
            return kind
    return None


def _match(
    kind: str, tool_input: Mapping[str, Any], titles: set[str], note_ids: set[str]
) -> tuple[str, str | None] | None:
    """tool 的 input → (比對鍵, notebook_id)；不相關回 None。

    只讀 title／note_id／notebook_id。"""
    if kind == "create_note":
        title = tool_input.get("title")
        if not isinstance(title, str) or _norm_title(title) not in titles:
            return None
        notebook = tool_input.get("notebook_id")
        return _norm_title(title), notebook if isinstance(notebook, str) else None
    note_id = str(tool_input.get("note_id") or "")
    return (note_id, None) if note_id in note_ids else None


def _claude_calls(line: Mapping[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """Claude Code transcript：assistant 訊息 content 裡的 tool_use。"""
    if line.get("type") != "assistant":
        return
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name, tool_input = block.get("name"), block.get("input")
        if isinstance(name, str) and isinstance(tool_input, dict):
            yield name, tool_input


def _codex_calls(line: Mapping[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """Codex rollout：`response_item` 的 `function_call`（arguments 是 JSON 字串）。"""
    payload = line.get("payload")
    if line.get("type") != "response_item" or not isinstance(payload, dict):
        return
    if payload.get("type") != "function_call":
        return
    name = f"{payload.get('namespace') or ''}{payload.get('name') or ''}"
    try:
        arguments = json.loads(payload.get("arguments") or "")
    except (TypeError, ValueError):
        return
    if isinstance(arguments, dict):
        yield name, arguments


def _codex_context_cwd(raw: bytes) -> str | None:
    """Codex 的 cwd 在 `session_meta`／`turn_context` 行。

    之後的 function_call 沿用最近一次看到的 cwd。"""
    head = raw[:160]
    if b'"session_meta"' not in head and b'"turn_context"' not in head:
        return None
    try:
        payload = json.loads(raw).get("payload")
    except (ValueError, AttributeError):
        return None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def scan_transcripts(
    roots: Iterable[Path], *, titles: set[str], note_ids: set[str]
) -> list[ToolUseHit]:
    """找標題在 `titles`（正規化後）的 create_note，與 note_id 在 `note_ids` 的
    update_note。支援 Claude Code transcript 與 Codex rollout 兩種格式。

    只取工具呼叫 input 的 title／note_id／notebook_id，以及該訊息的
    cwd／timestamp；tool_result（Codex 的 function_call_output）與其他內容一概不看。
    先以位元組子字串粗篩，只解析可能相關的行。
    """
    hits: list[ToolUseHit] = []
    for path in iter_transcripts(roots):
        fallback_cwd: str | None = None
        fallback_loaded = False
        context_cwd: str | None = None
        try:
            fh = path.open("rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                seen_cwd = _codex_context_cwd(raw)
                if seen_cwd is not None:
                    context_cwd = seen_cwd
                    continue
                if not any(hint in raw for hint in _LINE_HINTS):
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(line, dict):
                    continue
                own_cwd = line.get("cwd") if isinstance(line.get("cwd"), str) else None
                ts = line.get("timestamp")
                calls = [*_claude_calls(line), *_codex_calls(line)]
                for name, tool_input in calls:
                    kind = _tool_kind(name)
                    matched = (
                        _match(kind, tool_input, titles, note_ids) if kind else None
                    )
                    if kind is None or matched is None:
                        continue
                    key, notebook = matched
                    cwd = own_cwd or context_cwd
                    if cwd is None and not fallback_loaded:
                        fallback_cwd = _file_cwd(path)
                        fallback_loaded = True
                    hits.append(
                        ToolUseHit(
                            tool=kind,
                            key=key,
                            cwd=cwd or fallback_cwd,
                            timestamp=ts if isinstance(ts, str) else None,
                            transcript=str(path),
                            notebook_id=notebook,
                        )
                    )
    return hits


# ── notebook mapping → vault 索引 ───────────────────────────────────


def vault_index(mapping: Mapping[str, Any]) -> dict[str, str]:
    """notebook mapping 中未略過的 vault：key／別名（正規化）→ vault key。"""
    index: dict[str, str] = {}

    def claim(name: str, key: str) -> None:
        existing = index.get(name)
        if existing is not None and existing != key:
            raise OrphanError(f"mapping 中 {name!r} 同時指向 {existing!r} 與 {key!r}")
        index[name] = key

    for entry in mapping.get("notebooks", []):
        key = entry.get("key")
        if entry.get("skip") or not isinstance(key, str) or not key.strip():
            continue
        key = canonical_key(key.strip())
        claim(key, key)
        for alias in entry.get("aliases") or ():
            claim(lookup_key(alias), key)
    return index


def notebook_vaults(mapping: Mapping[str, Any]) -> dict[str, str]:
    """notebook id → vault key（未略過、有 key 的）。"""
    return {
        e["on_id"]: canonical_key(e["key"].strip())
        for e in mapping.get("notebooks", [])
        if not e.get("skip") and isinstance(e.get("key"), str) and e["key"].strip()
    }


# ── 孤兒 mapping ─────────────────────────────────────────────────────


@dataclass
class _Resolver:
    index: Mapping[str, str]
    notebooks: Mapping[str, str]
    binder: Callable[[str], str]
    _cache: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

    def binding(self, cwd: str | None) -> tuple[str | None, str | None]:
        """cwd → (binding key, 錯誤原因)。"""
        if not cwd:
            return None, "訊息沒有 cwd"
        if cwd not in self._cache:
            try:
                self._cache[cwd] = (self.binder(cwd), None)
            except NotADirectoryError:
                self._cache[cwd] = (None, "cwd 目錄已不存在")
            except Exception as exc:  # noqa: BLE001 — 解析失敗就交人工
                self._cache[cwd] = (None, f"binding 失敗：{type(exc).__name__}")
        return self._cache[cwd]

    def candidate(self, hit: ToolUseHit) -> dict[str, Any]:
        key, error = self.binding(hit.cwd)
        vault = self.index.get(lookup_key(key)) if key else None
        item: dict[str, Any] = {
            "tool": hit.tool,
            "cwd": hit.cwd,
            "binding_key": key,
            "vault": vault,
            "timestamp": hit.timestamp,
            "transcript": hit.transcript,
        }
        if error:
            item["error"] = error
        if hit.notebook_id is not None:
            item["notebook_id"] = hit.notebook_id
            item["notebook_vault"] = self.notebooks.get(hit.notebook_id)
        return item


def _within_window(candidate: Mapping[str, Any], created: datetime | None) -> bool:
    ts = _parse_ts(candidate.get("timestamp"))
    return created is not None and ts is not None and abs(ts - created) <= MATCH_WINDOW


def _decide(
    creates: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    created: datetime | None,
) -> tuple[str | None, str | None, str | None]:
    """回傳 (vault, basis, 需人工的原因)。"""
    if creates:
        vaults = {c["vault"] for c in creates}
        basis = BASIS_CREATE_TITLE
        if len(vaults) > 1:
            near = [c for c in creates if _within_window(c, created)]
            vaults = {c["vault"] for c in near}
            basis = BASIS_CREATE_TITLE_TIME
            if len(vaults) != 1:
                return None, None, "同標題的 create_note 指向多個 vault，時間也無法區分"
        (vault,) = vaults
        if vault is None:
            keys = sorted({c["binding_key"] or "?" for c in creates})
            return None, None, f"cwd 算出的 key 不在 mapping 的 vault／別名中：{keys}"
        disagree = sorted({u["vault"] or "?" for u in updates} - {vault})
        if disagree:
            return None, None, f"update_note 的 cwd 指向其他 vault：{disagree}"
        return vault, basis, None
    if updates:
        vaults = {u["vault"] for u in updates}
        if len(vaults) != 1:
            return None, None, "沒有 create_note；update_note 指向多個 vault"
        (vault,) = vaults
        if vault is None:
            keys = sorted({u["binding_key"] or "?" for u in updates})
            return None, None, f"cwd 算出的 key 不在 mapping 的 vault／別名中：{keys}"
        return vault, BASIS_UPDATE_ID, None
    return None, None, "transcript 找不到建立或更新這則 note 的 tool_use"


def build_orphan_map(
    orphans: Sequence[OrphanInput],
    mapping: Mapping[str, Any],
    roots: Sequence[Path],
    *,
    exclude: Iterable[str] = (),
    binder: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    excluded = set(exclude)
    unknown = sorted(excluded - {o.id for o in orphans})
    if unknown:
        raise OrphanError(f"--exclude 的 id 不在孤兒清單中：{unknown}")
    wanted = [o for o in orphans if o.id not in excluded]
    hits = scan_transcripts(
        roots,
        titles={_norm_title(o.title) for o in wanted},
        note_ids={o.id for o in wanted},
    )
    resolver = _Resolver(
        index=vault_index(mapping),
        notebooks=notebook_vaults(mapping),
        binder=binder or (lambda cwd: resolve_binding(cwd).key),
    )
    by_title: dict[str, list[ToolUseHit]] = {}
    by_id: dict[str, list[ToolUseHit]] = {}
    for hit in hits:
        target = by_title if hit.tool == "create_note" else by_id
        target.setdefault(hit.key, []).append(hit)

    entries: list[dict[str, Any]] = []
    for orphan in orphans:
        entry: dict[str, Any] = {
            "id": orphan.id,
            "title": orphan.title,
            "created": orphan.created,
            "vault": None,
            "basis": None,
            "skip": False,
            "needs_review": False,
            "review_reason": None,
            "candidates": [],
        }
        if orphan.id in excluded:
            entry.update(skip=True, basis=BASIS_EXCLUDED)
            entries.append(entry)
            continue
        creates = [
            resolver.candidate(h) for h in by_title.get(_norm_title(orphan.title), [])
        ]
        updates = [resolver.candidate(h) for h in by_id.get(orphan.id, [])]
        entry["candidates"] = creates + updates
        vault, basis, reason = _decide(creates, updates, _parse_ts(orphan.created))
        entry.update(vault=vault, basis=basis)
        if reason:
            entry.update(needs_review=True, review_reason=reason)
        entries.append(entry)
    return {
        "format": ORPHAN_MAP_FORMAT,
        "source": "open-notebook",
        "kind": "orphans",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "orphans": entries,
    }


def orphan_map_stats(orphan_map: Mapping[str, Any]) -> dict[str, Any]:
    """stdout 用：只有筆數、vault 與 id（不含標題、cwd 以外的內容）。"""
    per_vault: dict[str, int] = {}
    by_basis: dict[str, int] = {}
    review: list[dict[str, Any]] = []
    excluded: list[str] = []
    for entry in orphan_map["orphans"]:
        if entry.get("skip"):
            excluded.append(entry["id"])
            continue
        if entry.get("needs_review"):
            review.append({"id": entry["id"], "reason": entry.get("review_reason")})
            continue
        per_vault[entry["vault"]] = per_vault.get(entry["vault"], 0) + 1
        by_basis[entry["basis"]] = by_basis.get(entry["basis"], 0) + 1
    return {
        "total": len(orphan_map["orphans"]),
        "excluded": excluded,
        "auto_assigned": sum(per_vault.values()),
        "per_vault": dict(sorted(per_vault.items())),
        "by_basis": dict(sorted(by_basis.items())),
        "needs_review": review,
    }


def validate_orphan_map(orphan_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (
        orphan_map.get("format") != ORPHAN_MAP_FORMAT
        or orphan_map.get("kind") != "orphans"
    ):
        raise OrphanError("孤兒 mapping 格式不符")
    entries = orphan_map.get("orphans")
    if not isinstance(entries, list):
        raise OrphanError("孤兒 mapping 缺 orphans 清單")
    seen: set[str] = set()
    for entry in entries:
        note_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(note_id, str) or not note_id or note_id in seen:
            raise OrphanError(f"孤兒 mapping id 缺漏或重複：{note_id!r}")
        seen.add(note_id)
    return entries


# ── 取回內容 ─────────────────────────────────────────────────────────


RECORD_FIELDS = ("id", "title", "content", "note_type", "created", "updated")


def _record(note: Mapping[str, Any], origin: str) -> dict[str, Any]:
    record = {name: note.get(name) for name in RECORD_FIELDS}
    record["notebooks"] = []
    record["origin"] = origin
    return record


def load_supplement(path: Path, ids: set[str]) -> dict[str, dict[str, Any]]:
    """手動從 SurrealDB 唯讀副本匯出的 JSONL。

    每行一個物件：id、title、content、created、updated。"""
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                raise OrphanError(f"supplement 第 {number} 行不是 JSON") from None
            note_id = item.get("id") if isinstance(item, dict) else None
            if note_id not in ids:
                raise OrphanError(
                    f"supplement 第 {number} 行的 id 不在孤兒 mapping：{note_id!r}"
                )
            missing = [
                k for k in ("title", "content", "created", "updated") if k not in item
            ]
            if missing:
                raise OrphanError(f"supplement {note_id} 缺欄位 {missing}")
            if note_id in records:
                raise OrphanError(f"supplement id 重複：{note_id!r}")
            records[note_id] = _record(item, "supplement")
    return records


def export_orphans(
    get: Callable[[str], tuple[int, Any]],
    orphan_map: Mapping[str, Any],
    out_dir: Path,
    *,
    quote: Callable[[str], str],
    supplement: Mapping[str, dict[str, Any]] | None = None,
    write_jsonl: Callable[[Path, Iterable[Mapping[str, Any]]], int],
    write_json: Callable[[Path, Any], None],
    sha256_file: Callable[[Path], str],
) -> dict[str, Any]:
    """未略過的孤兒逐筆 `GET /api/notes/{id}`；取不到的用 supplement 補，
    仍缺者列 unavailable。"""
    entries = validate_orphan_map(orphan_map)
    supplement = supplement or {}
    records: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    origins = {"rest": 0, "supplement": 0}
    for entry in entries:
        if entry.get("skip"):
            continue
        note_id = entry["id"]
        status, data = get(f"/api/notes/{quote(note_id)}")
        if 200 <= status < 300 and isinstance(data, dict) and data.get("id") == note_id:
            records.append(_record(data, "rest"))
            origins["rest"] += 1
            continue
        if note_id in supplement:
            records.append(supplement[note_id])
            origins["supplement"] += 1
            continue
        unavailable.append({"id": note_id, "status": status})
    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_jsonl(out_dir / ORPHANS_FILE, records)
    manifest = {
        "format": ORPHAN_MAP_FORMAT,
        "source": "open-notebook",
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {"records": written, **origins, "unavailable": len(unavailable)},
        "unavailable": unavailable,
        "files": {ORPHANS_FILE: sha256_file(out_dir / ORPHANS_FILE)},
    }
    write_json(out_dir / ORPHANS_MANIFEST_FILE, manifest)
    return manifest


def load_orphan_records(
    export_dir: Path, sha256_file: Callable[[Path], str]
) -> list[dict[str, Any]]:
    """讀回 `orphans.jsonl` 並驗雜湊與筆數；沒有這個檔回空清單。"""
    manifest_path = export_dir / ORPHANS_MANIFEST_FILE
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise OrphanError(f"無法讀取 {ORPHANS_MANIFEST_FILE}：{exc}") from None
    path = export_dir / ORPHANS_FILE
    if sha256_file(path) != manifest["files"][ORPHANS_FILE]:
        raise OrphanError(f"{ORPHANS_FILE} 雜湊與 manifest 不符")
    with path.open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if len(rows) != manifest["counts"]["records"]:
        raise OrphanError(f"{ORPHANS_FILE} 筆數與 manifest 不符")
    return rows


def default_transcript_dirs() -> list[Path]:
    """Claude Code transcript 與 Codex rollout 的預設位置（存在的才用）。

    Claude Code 預設只保留約 30 天的 transcript；更早的 note 多半只在 Codex
    rollout 找得到。
    """
    home = Path(os.path.expanduser("~"))
    candidates = [home / ".claude" / "projects", home / ".codex" / "sessions"]
    return [p for p in candidates if p.is_dir()]
