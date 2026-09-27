"""Open Notebook → Lore Vault 匯入（T-33～T-37）。

    python -m lore_vault.cli.import_on export --out DIR [--base-url URL]
    python -m lore_vault.cli.import_on map --export DIR [--out FILE]
                                           [--search-root DIR ...]
    python -m lore_vault.cli.import_on import --export DIR --mapping FILE --db PATH
                                              [--report FILE] [--allow-unreviewed]
    python -m lore_vault.cli.import_on estimate --db PATH [--config PATH]
    python -m lore_vault.cli.import_on orphans-map --orphans FILE --mapping FILE
                                                   --out FILE [--projects-dir DIR ...]
                                                   [--exclude ID ...]
    python -m lore_vault.cli.import_on export-orphans --orphans-map FILE
                                                      --export DIR [--base-url URL]
                                                      [--supplement FILE]
    （import 另加 --orphans-map FILE 匯入孤兒）

資料流（ON 1.14.0 REST API，只用 GET）：
1. `GET /api/notebooks`：每本的 `note_count`（SurrealDB `count(<-artifact.in)`）即 ON 端
   原始筆數。
2. `GET /api/notes?notebook_id=`：每本的 note id 與歸屬。這個端點**不回內容**
   （上游 `get_notes(include_content=False)`），筆數必須等於 `note_count`。
3. `GET /api/notes`：全量清單（含內容）。能用就用來找孤兒 note；目前實機因某則
   note 含 null byte 回 500，此時改逐筆 `GET /api/notes/{id}` 取內容，孤兒無法列舉。
ON API 沒有分頁參數：list 端點一次回全部，所以防線是「筆數必須等於 ON 自己回報的數字」。

隱私：note 內容含商業原文。本工具只把內容寫進使用者指定的檔案與資料庫；
stdout／stderr 只印統計（筆數、id、雜湊），不印標題以外的內容。
所有輸出路徑（匯出目錄、mapping、報告、資料庫）不可位於本 repo 內。

note id 直接沿用 ON 原 id（形如 `note:xxxx`，自帶 namespace，不與新系統的
uuid hex 相撞）：可追溯、重跑天然冪等，連結也能在寫入前就解析成 id。
對帳清單另存在 `import_sources`（來源 id、內容雜湊、匯入當下的 updated）。
管理指令刪除過的 note 有墓碑（`note_tombstones`）：重跑匯入時跳過，不匯回，
報告列在 `deleted_skipped`；來源 note 全部已刪除的 vault 也不重建。
作者（A22）：新寫入與依來源更新的 note 一律 `author`／`updated_by` = `legacy`、
principal = 服務設定的 principal（`LORE_VAULT_PRINCIPAL`，D12；
未設為 `DEFAULT_PRINCIPAL`；本工具直接寫庫，沿用唯一的憑證主體）；
重跑冪等（未變動的 note 不改寫）。既有 note 被「採用」（adopted）時不改其作者。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from lore_vault.binding import folder_key, lookup_key, resolve_binding
from lore_vault.config import configured_principal
from lore_vault.notes import links as links_rules
from lore_vault.schema import (
    AUTHOR_LEGACY,
    DEFAULT_PRINCIPAL,
    SPACE_DEV,
    Note,
    Vault,
    canonical_key,
)
from lore_vault.schema.chars import sanitize_text
from lore_vault.storage import imports
from lore_vault.storage.db import connect, transaction
from lore_vault.storage.notes import insert_note, update_note_if
from lore_vault.storage.timeutil import format_utc
from lore_vault.storage.vaults import list_vaults, upsert_vault

from . import on_orphans

SOURCE = "open-notebook"
EXPORT_FORMAT = 1
MAPPING_FORMAT = 1
DEFAULT_BASE_URL = "http://localhost:5055"
PASSWORD_ENV = "OPEN_NOTEBOOK_PASSWORD"
NOTEBOOKS_FILE = "notebooks.jsonl"
NOTES_FILE = "notes.jsonl"
MANIFEST_FILE = "manifest.json"
DEFAULT_MAPPING_FILE = "mapping.json"
DEFAULT_REPORT_FILE = "import-report.json"
GLOBAL_KEY = "global"
PM_PREFIX = "[PM]"

_BIND = re.compile(r"\[bind:\s*([^\]\s]+)\s*\]", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_FRACTION = re.compile(r"(\.\d{6})\d+")
_ERROR_EXCERPT = 160

# (url, headers, timeout) -> (status, body)
Getter = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


class OnImportError(Exception):
    """匯出／匯入無法繼續（筆數不符、檔案損毀、mapping 不完整等）。

    訊息不含 note 內容。
    """


# ── 共用 ────────────────────────────────────────────────────────────


def urllib_getter(
    url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OnImportError(f"無法連線 {url}：{reason}") from None


def _repo_root() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


def _check_outside_repo(path: Path, what: str) -> Path:
    """輸出含商業原文，不可落在 repo 內（避免誤進版控）。"""
    resolved = path.expanduser().resolve()
    root = _repo_root()
    if root is not None and resolved.is_relative_to(root):
        raise OnImportError(f"{what} 不可放在 repo 內：{resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    tmp = path.with_name(path.name + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    os.replace(tmp, path)
    return count


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def norm_title(title: str) -> str:
    """標題比對形式：壓空白 + casefold（同 notes/service.py 的查重規則）。"""
    return _WHITESPACE.sub(" ", title).strip().casefold()


# ── T-33 匯出 ────────────────────────────────────────────────────────


@dataclass
class OnClient:
    base_url: str = DEFAULT_BASE_URL
    getter: Getter = urllib_getter
    password: str | None = None
    timeout: float = 30.0

    def get(self, path: str) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if self.password:
            headers["Authorization"] = f"Bearer {self.password}"
        status, body = self.getter(
            self.base_url.rstrip("/") + path, headers, self.timeout
        )
        if not 200 <= status < 300:
            detail = body.decode("utf-8", errors="replace").strip()[:_ERROR_EXCERPT]
            return status, detail
        try:
            return status, json.loads(body)
        except ValueError:
            raise OnImportError(f"GET {path} 回應不是 JSON") from None

    def get_ok(self, path: str) -> Any:
        status, data = self.get(path)
        if not 200 <= status < 300:
            raise OnImportError(f"GET {path} → HTTP {status}：{data}")
        return data


def _quote_id(record_id: str) -> str:
    return urllib.parse.quote(record_id, safe=":")


def _require_list(path: str, data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise OnImportError(f"GET {path} 回應不是物件陣列")
    return data


def export_on(client: OnClient, out_dir: Path) -> dict[str, Any]:
    """全量匯出成中繼 JSONL；任何筆數不一致都拋 `OnImportError`、不寫檔。"""
    errors: list[str] = []
    notebooks = _require_list("/api/notebooks", client.get_ok("/api/notebooks"))
    nb_ids = [str(nb.get("id") or "") for nb in notebooks]
    if any(not i for i in nb_ids) or len(set(nb_ids)) != len(nb_ids):
        raise OnImportError("notebook id 缺漏或重複")

    membership: dict[str, list[str]] = {}
    duplicate_edges: list[dict[str, Any]] = []
    member_edges = 0
    for nb in notebooks:
        path = f"/api/notes?notebook_id={_quote_id(nb['id'])}"
        listing = _require_list(path, client.get_ok(path))
        ids = [str(n.get("id") or "") for n in listing]
        expected = nb.get("note_count")
        member_edges += len(ids)
        if not isinstance(expected, int) or len(ids) != expected:
            errors.append(
                f"notebook {nb['id']}：列出 {len(ids)} 則，"
                f"ON 回報 note_count={expected}"
            )
        seen: set[str] = set()
        for note_id in ids:
            if not note_id:
                errors.append(f"notebook {nb['id']}：有 note 缺 id")
                continue
            if note_id in seen:
                duplicate_edges.append({"note": note_id, "notebook": nb["id"]})
                continue
            seen.add(note_id)
            membership.setdefault(note_id, []).append(nb["id"])

    status, full = client.get("/api/notes")
    full_by_id: dict[str, dict[str, Any]] | None = None
    if 200 <= status < 300:
        full_list = _require_list("/api/notes", full)
        full_by_id = {}
        for note in full_list:
            note_id = str(note.get("id") or "")
            if not note_id or note_id in full_by_id:
                errors.append(f"全量清單 note id 缺漏或重複：{note_id!r}")
                continue
            full_by_id[note_id] = note
        stray = sorted(set(membership) - set(full_by_id))
        if stray:
            errors.append(
                f"{len(stray)} 則 notebook 內的 note 不在全量清單：{stray[:5]}"
            )
    if errors:
        raise OnImportError("匯出筆數不一致：\n" + "\n".join(errors))

    records: list[dict[str, Any]] = []
    fetched = 0
    ordered_ids = list(membership)
    orphans: list[str] = []
    if full_by_id is not None:
        orphans = [i for i in full_by_id if i not in membership]
        ordered_ids += orphans
    for note_id in ordered_ids:
        if full_by_id is not None:
            note = full_by_id[note_id]
        else:
            note = client.get_ok(f"/api/notes/{_quote_id(note_id)}")
            fetched += 1
            if not isinstance(note, dict) or note.get("id") != note_id:
                raise OnImportError(f"GET /api/notes/{note_id} 回傳的 id 不符")
        records.append(
            {
                "id": note_id,
                "title": note.get("title"),
                "content": note.get("content"),
                "note_type": note.get("note_type"),
                "created": note.get("created"),
                "updated": note.get("updated"),
                "notebooks": membership.get(note_id, []),
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    nb_rows = [
        {
            "id": nb["id"],
            "name": nb.get("name") or "",
            "description": nb.get("description") or "",
            "archived": bool(nb.get("archived", False)),
            "created": nb.get("created"),
            "updated": nb.get("updated"),
            "note_count": nb.get("note_count"),
        }
        for nb in notebooks
    ]
    _write_jsonl(out_dir / NOTEBOOKS_FILE, nb_rows)
    written = _write_jsonl(out_dir / NOTES_FILE, records)
    # 寫完重讀一次：檔案筆數必須等於應有筆數
    reread = len(_read_jsonl(out_dir / NOTES_FILE))
    expected_total = len(membership) + len(orphans)
    if written != expected_total or reread != expected_total:
        raise OnImportError(
            f"匯出檔筆數不符：應有 {expected_total}、寫入 {written}、重讀 {reread}"
        )
    multi = [
        {"id": i, "notebooks": nbs} for i, nbs in membership.items() if len(nbs) > 1
    ]
    manifest = {
        "format": EXPORT_FORMAT,
        "source": SOURCE,
        "base_url": client.base_url,
        "exported_at": format_utc(datetime.now(UTC)),
        "counts": {
            "notebooks": len(nb_rows),
            "on_note_count_total": sum(int(nb["note_count"]) for nb in nb_rows),
            "member_edges": member_edges,
            "notes_in_notebooks": len(membership),
            "orphans": len(orphans),
            "notes": expected_total,
            "fetched_individually": fetched,
        },
        "all_notes_endpoint": {
            "status": status,
            "count": None if full_by_id is None else len(full_by_id),
            # 全量端點失敗時孤兒 note（不屬於任何 notebook）無法經 REST 列舉
            "orphans_enumerable": full_by_id is not None,
            "error": None if full_by_id is not None else str(full)[:_ERROR_EXCERPT],
        },
        "multi_membership": multi,
        "duplicate_edges": duplicate_edges,
        "orphans": orphans,
        "files": {
            NOTEBOOKS_FILE: _sha256_file(out_dir / NOTEBOOKS_FILE),
            NOTES_FILE: _sha256_file(out_dir / NOTES_FILE),
        },
    }
    _write_json(out_dir / MANIFEST_FILE, manifest)
    return manifest


@dataclass(frozen=True)
class ExportData:
    manifest: dict[str, Any]
    notebooks: list[dict[str, Any]]
    notes: list[dict[str, Any]]


def load_export(export_dir: Path) -> ExportData:
    """讀回匯出並驗證檔案雜湊與筆數（避免拿到半份或被改過的匯出）。"""
    try:
        manifest = json.loads((export_dir / MANIFEST_FILE).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise OnImportError(f"無法讀取 {MANIFEST_FILE}：{exc}") from None
    if manifest.get("format") != EXPORT_FORMAT or manifest.get("source") != SOURCE:
        raise OnImportError("manifest 格式或來源不符")
    for name, digest in manifest["files"].items():
        if _sha256_file(export_dir / name) != digest:
            raise OnImportError(f"{name} 雜湊與 manifest 不符（檔案被改過或不完整）")
    notebooks = _read_jsonl(export_dir / NOTEBOOKS_FILE)
    notes = _read_jsonl(export_dir / NOTES_FILE)
    counts = manifest["counts"]
    if len(notebooks) != counts["notebooks"] or len(notes) != counts["notes"]:
        raise OnImportError("匯出檔筆數與 manifest 不符")
    return ExportData(manifest, notebooks, notes)


# ── T-34 綁定 mapping ────────────────────────────────────────────────


def _display_from_name(name: str) -> str:
    stripped = name.strip()
    if stripped.startswith(PM_PREFIX):
        return stripped[len(PM_PREFIX) :].strip() or stripped
    return stripped


def _is_global(name: str) -> bool:
    return _display_from_name(name).casefold().startswith("global")


def _suggest_bindings(display: str, roots: Sequence[Path]) -> list[dict[str, str]]:
    """在搜尋根目錄下找同名資料夾（深度 ≤ 2），以 git remote 算出建議 key。"""
    target = display.casefold()
    found: list[dict[str, str]] = []
    for root in roots:
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith("."):
                    continue
                if entry.name.casefold() == target:
                    binding = resolve_binding(entry.path)
                    found.append(
                        {"path": entry.path, "key": binding.key, "via": binding.source}
                    )
                elif depth + 1 < 2:
                    stack.append((Path(entry.path), depth + 1))
    return found


def build_mapping(
    export: ExportData, *, search_roots: Sequence[Path] = ()
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for nb in export.notebooks:
        name = nb["name"]
        display = _display_from_name(name)
        binds = sorted({canonical_key(b) for b in _BIND.findall(nb["description"])})
        entry: dict[str, Any] = {
            "on_id": nb["id"],
            "name": name,
            "note_count": nb["note_count"],
            "key": None,
            "display": display,
            "kind": "repo",
            "aliases": [],
            "basis": None,
            "needs_review": False,
            "review_reason": None,
            "skip": False,
        }
        if len(binds) == 1:
            entry.update(key=binds[0], basis="bind")
        elif len(binds) > 1:
            entry.update(
                key=binds[0],
                basis="bind",
                needs_review=True,
                review_reason=f"description 有多個 [bind:] 標記：{binds}",
            )
        elif _is_global(name):
            entry.update(key=GLOBAL_KEY, kind="global", basis="global")
        elif name.strip().startswith(PM_PREFIX):
            entry.update(
                key=folder_key(display),
                basis="name",
                needs_review=True,
                review_reason="無 [bind:] 標記，以 [PM] 名稱推 folder key",
            )
            suggestions = _suggest_bindings(display, search_roots)
            if suggestions:
                entry["suggestions"] = suggestions
        else:
            entry.update(
                basis="none",
                needs_review=True,
                review_reason="名稱不是 [PM] <display> 形式，需人工指定 key",
            )
        entries.append(entry)

    by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["key"]:
            by_key.setdefault(entry["key"], []).append(entry)
    for key, group in by_key.items():
        if len(group) > 1:
            names = [g["on_id"] for g in group]
            for g in group:
                g["needs_review"] = True
                reason = f"與其他 notebook 共用 key {key!r}：{names}"
                g["review_reason"] = "；".join(
                    r for r in (g["review_reason"], reason) if r
                )

    key_of = {e["on_id"]: e["key"] for e in entries}
    assignments: dict[str, Any] = {}
    for multi in export.manifest.get("multi_membership", []):
        nbs = multi["notebooks"]
        keys = {key_of.get(nb) for nb in nbs}
        assignments[multi["id"]] = {
            "notebooks": nbs,
            "assigned": nbs[0],
            "needs_review": len(keys) > 1,
        }
    return {
        "format": MAPPING_FORMAT,
        "source": SOURCE,
        "exported_at": export.manifest.get("exported_at"),
        "notebooks": entries,
        "note_assignments": assignments,
        "orphans": list(export.manifest.get("orphans", [])),
    }


def review_items(mapping: Mapping[str, Any]) -> list[str]:
    items = [
        f"notebook {e['on_id']}（{e['name']}）→ {e['key']}：{e['review_reason']}"
        for e in mapping["notebooks"]
        if e.get("needs_review")
    ]
    items += [
        f"note {note_id} 屬於多本 notebook {a['notebooks']}，暫定歸 {a['assigned']}"
        for note_id, a in mapping.get("note_assignments", {}).items()
        if a.get("needs_review")
    ]
    return items


# ── 時間戳 ───────────────────────────────────────────────────────────


def to_utc_ms(value: Any) -> tuple[str, str | None]:
    """ON 時間戳 → `YYYY-MM-DDTHH:MM:SS.sssZ`。回傳 (結果, 異常種類或 None)。

    無時區：當作 UTC 並標 `naive`；非 UTC 時區：換算並標 `offset`。無法解析拋錯。
    """
    if not isinstance(value, str) or not value.strip():
        raise OnImportError(f"時間戳缺漏：{value!r}")
    text = _FRACTION.sub(r"\1", value.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise OnImportError(f"無法解析時間戳：{value!r}") from None
    anomaly = None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
        anomaly = "naive"
    elif parsed.utcoffset() != UTC.utcoffset(None):
        anomaly = "offset"
    return format_utc(parsed), anomaly


# ── T-35 連結 ────────────────────────────────────────────────────────


# 抽取與比對規則與服務端寫入共用（`lore_vault.notes.links`）
link_targets = links_rules.link_targets
_target_candidates = links_rules.target_candidates


@dataclass(frozen=True)
class LinkResult:
    ids: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]


class LinkResolver:
    """同 vault 內依標題解析；跨 vault 或同 vault 歧義保留原文並列報告。"""

    def __init__(self) -> None:
        # norm_title → {(vault, note_id)}
        self._index: dict[str, set[tuple[str, str]]] = {}

    def add(self, vault: str, note_id: str, title: str) -> None:
        self._index.setdefault(norm_title(title), set()).add((vault, note_id))

    def resolve(self, vault: str, note_id: str, body: str) -> LinkResult:
        ids: list[str] = []
        entries: list[dict[str, Any]] = []
        for raw in link_targets(body):
            status = "unresolved"
            candidates: list[str] = []
            for form in _target_candidates(raw):
                hits = self._index.get(form, set())
                same = sorted(i for v, i in hits if v == vault)
                other = sorted({v for v, _ in hits if v != vault})
                if len(same) == 1:
                    status, candidates = "resolved", same
                elif len(same) > 1:
                    status, candidates = "ambiguous", same
                elif other:
                    status, candidates = "cross_vault", other
                else:
                    continue
                break
            if status == "resolved" and candidates[0] == note_id:
                status = "self"
            if status == "resolved" and candidates[0] not in ids:
                ids.append(candidates[0])
            entries.append(
                {
                    "note": note_id,
                    "vault": vault,
                    "target": raw,
                    "status": status,
                    "candidates": candidates,
                }
            )
        return LinkResult(tuple(ids), tuple(entries))


# ── 匯入 ────────────────────────────────────────────────────────────


@dataclass
class PlannedNote:
    source_id: str
    vault: str
    title: str
    body: str
    created: str
    updated: str
    content_sha256: str


def _validate_mapping(
    export: ExportData, mapping: Mapping[str, Any], *, allow_unreviewed: bool
) -> dict[str, dict[str, Any]]:
    if mapping.get("format") != MAPPING_FORMAT or mapping.get("source") != SOURCE:
        raise OnImportError("mapping 格式或來源不符")
    entries = {e["on_id"]: e for e in mapping["notebooks"]}
    missing = [nb["id"] for nb in export.notebooks if nb["id"] not in entries]
    if missing:
        raise OnImportError(f"mapping 缺少 notebook：{missing}")
    problems: list[str] = []
    kinds: dict[str, str] = {}
    for entry in entries.values():
        if entry.get("skip"):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            problems.append(f"notebook {entry['on_id']} 沒有 key")
            continue
        entry["key"] = canonical_key(key.strip())
        if entry.get("kind", "repo") not in ("repo", "global"):
            problems.append(f"notebook {entry['on_id']} kind 不合法")
        previous = kinds.setdefault(entry["key"], entry.get("kind", "repo"))
        if previous != entry.get("kind", "repo"):
            problems.append(f"key {entry['key']!r} 的 kind 前後不一致")
    unreviewed = review_items(mapping)
    if unreviewed and not allow_unreviewed:
        problems.append(
            f"{len(unreviewed)} 項待人工確認（改 mapping 後把 needs_review 設 false，"
            "或加 --allow-unreviewed）"
        )
    if problems:
        raise OnImportError("mapping 不可用：\n" + "\n".join(problems))
    return entries


def _planned_note(
    note: Mapping[str, Any], vault: str, report: dict[str, Any]
) -> PlannedNote:
    """ON 紀錄 → 待匯入 note：時間戳正規化、空標題補位，並清理禁用控制字元。

    ON 端資料不可拒收（會丟資料）：NUL 換成可見的 `\\0`、其他 C0 換成 `\\xNN`，
    逐則記在報告 `sanitized`。清理在算內容雜湊之前，重跑匯入結果一致。
    """
    anomalies: list[dict[str, Any]] = report["timestamp_anomalies"]
    created, created_anomaly = to_utc_ms(note["created"])
    updated, updated_anomaly = to_utc_ms(note["updated"])
    for field, kind, raw in (
        ("created", created_anomaly, note["created"]),
        ("updated", updated_anomaly, note["updated"]),
    ):
        if kind:
            anomalies.append(
                {"id": note["id"], "field": field, "kind": kind, "raw": raw}
            )
    if updated < created:
        anomalies.append(
            {
                "id": note["id"],
                "field": "updated",
                "kind": "before_created",
                "raw": note["updated"],
            }
        )
        updated = created
    title = note.get("title")
    if not isinstance(title, str) or not title.strip():
        anomalies.append({"id": note["id"], "field": "title", "kind": "empty"})
        title = f"(無標題 {note['id']})"
    body = note.get("content")
    if not isinstance(body, str):
        anomalies.append({"id": note["id"], "field": "content", "kind": "null"})
        body = ""
    title, title_fixed = sanitize_text(title)
    body, body_fixed = sanitize_text(body)
    if title_fixed or body_fixed:
        sanitized = report["sanitized"]
        sanitized["notes"].append(
            {"id": note["id"], "title": title_fixed, "body": body_fixed}
        )
        sanitized["chars"] += title_fixed + body_fixed
    return PlannedNote(
        source_id=note["id"],
        vault=vault,
        title=title,
        body=body,
        created=created,
        updated=updated,
        content_sha256=imports.content_sha256(title, body),
    )


@dataclass(frozen=True)
class OrphanPlan:
    """孤兒 mapping（`orphans-map` 產生、人工確認過）與取回的內容紀錄。"""

    entries: list[dict[str, Any]]
    records: list[dict[str, Any]]


def orphan_review_items(orphans: OrphanPlan | None) -> list[str]:
    if orphans is None:
        return []
    return [
        f"孤兒 note {e['id']}：{e.get('review_reason') or '待確認'}"
        for e in orphans.entries
        if not e.get("skip") and e.get("needs_review")
    ]


def _plan(
    export: ExportData,
    mapping: Mapping[str, Any],
    entries: Mapping[str, Mapping[str, Any]],
    report: dict[str, Any],
    orphans: OrphanPlan | None = None,
) -> list[PlannedNote]:
    assignments = mapping.get("note_assignments", {})
    planned: list[PlannedNote] = []
    # 不屬任何 notebook 的 note：全量端點可用時在 notes.jsonl，否則來自 orphans.jsonl
    orphan_records: dict[str, Mapping[str, Any]] = {}
    for note in export.notes:
        nbs = note["notebooks"]
        if not nbs:
            orphan_records.setdefault(note["id"], note)
            continue
        if len(nbs) == 1:
            chosen = nbs[0]
        else:
            assigned = assignments.get(note["id"], {}).get("assigned")
            if assigned not in nbs:
                raise OnImportError(
                    f"note {note['id']} 屬於多本 notebook，mapping 沒有有效的 assigned"
                )
            chosen = assigned
        entry = entries[chosen]
        if entry.get("skip"):
            report["skipped"]["notebook_skipped"] += 1
            continue
        planned.append(_planned_note(note, entry["key"], report))

    in_notebooks = {n["id"] for n in export.notes if n["notebooks"]}
    for record in orphans.records if orphans is not None else ():
        if record.get("id") in in_notebooks:
            raise OnImportError(f"孤兒紀錄 {record.get('id')} 其實屬於 notebook")
        orphan_records[record["id"]] = record
    orphan_report = report["orphans"]
    by_id = {e["id"]: e for e in orphans.entries} if orphans is not None else {}
    index = on_orphans.vault_index(mapping) if orphans is not None else {}
    targets: dict[str, str] = {}
    bad_vaults: list[str] = []
    for entry in by_id.values():
        note_id = entry["id"]
        if entry.get("skip"):
            report["skipped"]["orphan_excluded"] += 1
            orphan_report["excluded"].append(note_id)
            continue
        vault_name = entry.get("vault")
        if not isinstance(vault_name, str) or not vault_name.strip():
            # 只有 --allow-unreviewed 才會走到這裡（否則驗證時已擋下）
            report["skipped"]["orphan_unassigned"] += 1
            orphan_report["unassigned"].append(note_id)
            continue
        vault = index.get(lookup_key(vault_name))
        if vault is None:
            bad_vaults.append(f"{note_id} → {vault_name!r}")
            continue
        targets[note_id] = vault
    if bad_vaults:
        raise OnImportError(
            "孤兒 note 的 vault 不是 mapping 中的 vault 或別名："
            + "、".join(bad_vaults)
        )
    no_record = [i for i in targets if i not in orphan_records]
    if no_record:
        raise OnImportError(
            f"孤兒 note 沒有內容紀錄（先跑 export-orphans，REST 取不到的用 "
            f"--supplement 補）：{no_record}"
        )
    for note_id, vault in targets.items():
        planned.append(_planned_note(orphan_records[note_id], vault, report))
        orphan_report["planned"].append(note_id)
    for note_id in orphan_records:
        if note_id not in by_id:
            report["skipped"]["orphan"] += 1
            orphan_report["unmapped"].append(note_id)
    return planned


def _new_report() -> dict[str, Any]:
    return {
        "source": SOURCE,
        "vaults": {"created": [], "existing": [], "deleted_skipped": []},
        "notes": {
            "planned": 0,
            "inserted": 0,
            "reinserted": 0,
            "adopted": 0,
            "updated_from_source": 0,
            "unchanged": 0,
            "modified_locally": [],
            "db_tampered": [],
            "conflicts": [],
        },
        # deleted：管理指令刪除過（有墓碑），不匯回
        # orphan：孤兒 mapping 沒列的孤兒；
        # orphan_excluded：孤兒 mapping 標 skip（測試／佔位）；
        # orphan_unassigned：--allow-unreviewed 下仍未指定 vault
        "skipped": {
            "orphan": 0,
            "orphan_excluded": 0,
            "orphan_unassigned": 0,
            "notebook_skipped": 0,
            "deleted": 0,
        },
        "orphans": {"planned": [], "excluded": [], "unassigned": [], "unmapped": []},
        # 清理掉控制字元的 note：{"id", "title": 替換數, "body": 替換數}
        "sanitized": {"notes": [], "chars": 0},
        "deleted_skipped": [],
        "per_vault": {},
        "review": [],
        "multi_membership": [],
        "timestamp_anomalies": [],
        "links": {"total": 0, "by_status": {}, "entries": []},
        "manifest": {},
    }


def run_import(
    conn: sqlite3.Connection,
    export: ExportData,
    mapping: Mapping[str, Any],
    *,
    allow_unreviewed: bool = False,
    orphans: OrphanPlan | None = None,
    principal: str = DEFAULT_PRINCIPAL,
) -> dict[str, Any]:
    """依 mapping 建 vault 與 note；回傳報告（含標題的明細只寫進報告檔）。

    `orphans`：孤兒 note 的歸屬與內容。一旦匯入過孤兒，之後每次重跑都要帶同一份，
    否則對帳清單會把它們移除（note 仍在，對帳改算「新系統新增」）。
    """
    entries = _validate_mapping(export, mapping, allow_unreviewed=allow_unreviewed)
    orphan_review = orphan_review_items(orphans)
    if orphan_review and not allow_unreviewed:
        raise OnImportError(
            f"孤兒 mapping 有 {len(orphan_review)} 項待人工確認（填 vault 後把 "
            "needs_review 設 false，或加 --allow-unreviewed）"
        )
    report = _new_report()
    report["review"] = review_items(mapping) + orphan_review
    report["multi_membership"] = [
        {"id": note_id, **a}
        for note_id, a in mapping.get("note_assignments", {}).items()
    ]
    planned = _plan(export, mapping, entries, report, orphans)
    report["notes"]["planned"] = len(planned)
    # 墓碑：管理指令刪掉的 note 不匯回。仍留在對帳清單與來源筆數裡，
    # 對帳才分得出「刻意刪除」與「漏匯」
    graves = imports.tombstones(conn, SOURCE)
    alive = [p for p in planned if not graves.covers(p.source_id, p.source_id)]
    alive_vaults = {p.vault for p in alive}
    buried_vaults = {p.vault for p in planned} - alive_vaults

    # vault：不存在才建（既有 vault 的別名與顯示名稱不動）；
    # 來源 note 全部已刪除的 vault（delete-vault --force 過）不重建
    # 匯入固定進 dev（A18：現有 notebook 全是開發記憶）；key 已屬於別的 space
    # 時明確失敗，不靜默當成既有 vault
    existing = {v.key: v for v in list_vaults(conn, space=None)}
    wanted: dict[str, dict[str, Any]] = {}
    for entry in entries.values():
        if not entry.get("skip"):
            wanted.setdefault(entry["key"], entry)
    for key, entry in sorted(wanted.items()):
        current = existing.get(key)
        if current is None and key in buried_vaults:
            report["vaults"]["deleted_skipped"].append(key)
            continue
        kind = entry.get("kind", "repo")
        if current is not None and current.space != SPACE_DEV:
            raise OnImportError(
                f"vault {key!r} 屬於 space {current.space!r}；匯入只寫入 dev"
            )
        if current is not None:
            if current.kind != kind:
                raise OnImportError(
                    f"vault {key!r} 已存在且 kind={current.kind}，mapping 要求 {kind}"
                )
            report["vaults"]["existing"].append(key)
            continue
        upsert_vault(
            conn,
            Vault(
                key=key,
                display=entry.get("display") or key,
                kind=kind,
                aliases=tuple(entry.get("aliases") or ()),
            ),
        )
        report["vaults"]["created"].append(key)

    # 對帳清單先落地（T-37）：之後漏寫任何一則 note，doctor 都看得出來
    prior = imports.manifest_rows(conn, SOURCE)
    counts = {key: 0 for key in wanted}
    for item in planned:
        counts[item.vault] += 1
    report["per_vault"] = dict(sorted(counts.items()))
    report["manifest"] = imports.record_manifest(
        conn,
        SOURCE,
        [
            imports.ManifestEntry(
                source_id=p.source_id,
                note_id=p.source_id,
                vault=p.vault,
                content_sha256=p.content_sha256,
                source_updated=p.updated,
            )
            for p in planned
        ],
        counts,
    )

    resolver = LinkResolver()
    for row in conn.execute("SELECT vault, id, title FROM notes"):
        resolver.add(row[0], row[1], row[2])
    for p in alive:
        resolver.add(p.vault, p.source_id, p.title)

    link_stats: dict[str, int] = {}
    notes_report = report["notes"]
    for p in sorted(planned, key=lambda x: (x.created, x.source_id)):
        if graves.covers(p.source_id, p.source_id):
            report["skipped"]["deleted"] += 1
            report["deleted_skipped"].append(p.source_id)
            continue
        links = resolver.resolve(p.vault, p.source_id, p.body)
        for entry in links.entries:
            link_stats[entry["status"]] = link_stats.get(entry["status"], 0) + 1
            if entry["status"] not in ("resolved", "self"):
                report["links"]["entries"].append(entry)
        report["links"]["total"] += len(links.entries)
        _import_one(conn, p, links.ids, prior.get(p.source_id), notes_report, principal)
    report["links"]["by_status"] = dict(sorted(link_stats.items()))
    return report


def _import_one(
    conn: sqlite3.Connection,
    p: PlannedNote,
    link_ids: tuple[str, ...],
    prior: imports.ManifestRow | None,
    out: dict[str, Any],
    principal: str,
) -> None:
    row = conn.execute(
        "SELECT vault, title, body, updated FROM notes WHERE id = ?", (p.source_id,)
    ).fetchone()
    if row is None:
        stored = insert_note(
            conn,
            p.vault,
            Note(
                id=p.source_id,
                vault=p.vault,
                title=p.title,
                body=p.body,
                created=p.created,
                updated=p.updated,
                links=link_ids,
                author=AUTHOR_LEGACY,
                principal=principal,
                updated_by=AUTHOR_LEGACY,
                updated_by_principal=principal,
            ),
            space=SPACE_DEV,
        )
        imports.mark_imported(conn, SOURCE, p.source_id, stored.updated)
        was_imported = prior is not None and prior.imported_updated is not None
        out["reinserted" if was_imported else "inserted"] += 1
        return
    vault, title, body, updated = row[0], row[1], row[2], row[3]
    if vault != p.vault:
        out["conflicts"].append(
            {
                "id": p.source_id,
                "reason": f"已存在於 vault {vault}，mapping 為 {p.vault}",
            }
        )
        return
    current_hash = imports.content_sha256(title, body)
    if prior is None or prior.imported_updated is None:
        if current_hash == p.content_sha256:
            imports.mark_imported(conn, SOURCE, p.source_id, updated)
            out["adopted"] += 1
        else:
            out["conflicts"].append(
                {"id": p.source_id, "reason": "id 已存在但不是本工具匯入、內容不同"}
            )
        return
    if updated != prior.imported_updated:
        # 匯入後已在新系統修改：不覆寫
        out["modified_locally"].append(p.source_id)
        return
    if current_hash == p.content_sha256:
        out["unchanged"] += 1
        return
    if prior.content_sha256 == p.content_sha256:
        # 來源沒變、資料庫內容卻變了且 updated 未推進：不自動修，交給人工
        out["db_tampered"].append(p.source_id)
        return
    with transaction(conn):
        stored = update_note_if(
            conn,
            p.vault,
            p.source_id,
            updated,
            {"title": p.title, "body": p.body, "links": link_ids, "summary": None},
            space=SPACE_DEV,
            now=p.updated,
            editor=(AUTHOR_LEGACY, principal),
        )
        if stored is None:
            raise OnImportError(f"note {p.source_id} 更新時版本衝突")
        imports.mark_imported(conn, SOURCE, p.source_id, stored.updated)
    out["updated_from_source"] += 1


def report_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """stdout 用的摘要：只有筆數與 id，不含標題、連結目標等內容。"""
    notes = report["notes"]
    links = report["links"]
    resolved = links["by_status"].get("resolved", 0)
    total = links["total"]
    return {
        "vaults_created": len(report["vaults"]["created"]),
        "vaults_existing": len(report["vaults"]["existing"]),
        "vaults_deleted_skipped": len(report["vaults"]["deleted_skipped"]),
        "per_vault": report["per_vault"],
        "notes": {k: (len(v) if isinstance(v, list) else v) for k, v in notes.items()},
        "skipped": report["skipped"],
        "orphans": {k: len(v) for k, v in report["orphans"].items()},
        "sanitized": {
            "notes": [n["id"] for n in report["sanitized"]["notes"]],
            "chars": report["sanitized"]["chars"],
        },
        "review_items": len(report["review"]),
        "multi_membership": len(report["multi_membership"]),
        "timestamp_anomalies": len(report["timestamp_anomalies"]),
        "links": {
            "total": total,
            "by_status": links["by_status"],
            "resolve_rate": round(resolved / total, 4) if total else None,
        },
        "manifest": report["manifest"],
    }


# ── T-36 估算 ───────────────────────────────────────────────────────

# D4 實測（2026-09-26，gpt-6-luna effort=low，約 615 input tokens）：約 2 秒、129 output
SAMPLE_SUMMARY_SECONDS = 2.0
SAMPLE_SUMMARY_OUTPUT_TOKENS = 129
# bge-m3 本機 Ollama 單則 embedding 的保守假設（秒）；未實測，--embed-seconds 可改
DEFAULT_EMBED_SECONDS = 0.5


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
        or 0x20000 <= code <= 0x2FFFF
    )


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 每字 1、其餘每 4 字元 1（不呼叫任何 API）。"""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_backfill(
    conn: sqlite3.Connection,
    *,
    summary_rate_per_minute: int,
    embed_rate_per_minute: int,
    summary_seconds: float = SAMPLE_SUMMARY_SECONDS,
    embed_seconds: float = DEFAULT_EMBED_SECONDS,
    system_prompt: str = "",
) -> dict[str, Any]:
    prompt_tokens = estimate_tokens(system_prompt)
    summary_rows = conn.execute(
        "SELECT title, body FROM notes WHERE summary IS NULL"
    ).fetchall()
    embed_rows = conn.execute(
        """
        SELECT n.title, n.body FROM notes n
        LEFT JOIN note_embeddings e ON e.note_seq = n.seq WHERE e.note_seq IS NULL
        """
    ).fetchall()
    summary_input = sum(
        prompt_tokens + estimate_tokens(f"標題：{r[0]}\n\n{r[1]}") for r in summary_rows
    )
    embed_input = sum(estimate_tokens(f"{r[0]}\n\n{r[1]}") for r in embed_rows)

    def per_item(latency: float, rate: int) -> float:
        return max(latency, 60.0 / rate) if rate > 0 else latency

    n_sum, n_emb = len(summary_rows), len(embed_rows)
    return {
        "pending": {"summary": n_sum, "embedding": n_emb},
        "summary": {
            "input_tokens_est": summary_input,
            "output_tokens_est": n_sum * SAMPLE_SUMMARY_OUTPUT_TOKENS,
            "avg_input_tokens_est": round(summary_input / n_sum) if n_sum else 0,
            "seconds_est": round(
                n_sum * per_item(summary_seconds, summary_rate_per_minute)
            ),
            "assumptions": {
                "seconds_per_call": summary_seconds,
                "rate_per_minute": summary_rate_per_minute,
                "output_tokens_per_call": SAMPLE_SUMMARY_OUTPUT_TOKENS,
                "reasoning_tokens": "未計（effort=low 仍可能產生）",
            },
        },
        "embedding": {
            "input_tokens_est": embed_input,
            "seconds_est": round(
                n_emb * per_item(embed_seconds, embed_rate_per_minute)
            ),
            "assumptions": {
                "seconds_per_call": embed_seconds,
                "rate_per_minute": embed_rate_per_minute,
            },
        },
        "token_heuristic": "CJK 每字 1、其餘每 4 字元 1",
    }


# ── 命令列 ──────────────────────────────────────────────────────────


def _print(out: TextIO, data: Any) -> None:
    out.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    out.flush()


def main(
    argv: Sequence[str] | None = None,
    *,
    getter: Getter = urllib_getter,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m lore_vault.cli.import_on")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="從 ON REST API 全量匯出（只用 GET）")
    p_export.add_argument("--out", required=True, help="匯出目錄（repo 外）")
    p_export.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p_export.add_argument("--timeout", type=float, default=30.0)

    p_map = sub.add_parser("map", help="產生 notebook → vault mapping")
    p_map.add_argument("--export", required=True, help="匯出目錄")
    p_map.add_argument(
        "--out", help=f"mapping 檔（預設 <export>/{DEFAULT_MAPPING_FILE}）"
    )
    p_map.add_argument(
        "--search-root",
        action="append",
        default=[],
        help="找同名 repo 目錄以建議 git remote key（可重複）",
    )

    p_import = sub.add_parser("import", help="依 mapping 匯入資料庫（冪等）")
    p_import.add_argument("--export", required=True)
    p_import.add_argument("--mapping", required=True)
    p_import.add_argument("--db", required=True)
    p_import.add_argument(
        "--report", help=f"報告檔（預設 <export>/{DEFAULT_REPORT_FILE}）"
    )
    p_import.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="mapping 仍有待確認項也照目前內容匯入（乾跑用）",
    )
    p_import.add_argument(
        "--orphans-map",
        help="孤兒 mapping（內容讀 <export>/orphans.jsonl）；"
        "匯入過孤兒後每次重跑都要帶",
    )

    p_omap = sub.add_parser(
        "orphans-map", help="依 transcript 推孤兒 note 的 vault（只讀 tool_use input）"
    )
    p_omap.add_argument(
        "--orphans",
        required=True,
        help="孤兒清單（JSON／JSONL／TSV：id、created、title）",
    )
    p_omap.add_argument(
        "--mapping", required=True, help="notebook mapping（map 的輸出）"
    )
    p_omap.add_argument("--out", required=True, help="孤兒 mapping 輸出檔（repo 外）")
    p_omap.add_argument(
        "--projects-dir",
        action="append",
        default=[],
        help="transcript 目錄，Claude Code 或 Codex 格式皆可（可重複；預設 "
        "~/.claude/projects 與 ~/.codex/sessions）",
    )
    p_omap.add_argument(
        "--exclude", action="append", default=[], help="不匯入的孤兒 id（可重複）"
    )

    p_oexp = sub.add_parser(
        "export-orphans", help="依孤兒 mapping 逐筆 GET /api/notes/{id} 取內容"
    )
    p_oexp.add_argument("--orphans-map", required=True)
    p_oexp.add_argument("--export", required=True, help="匯出目錄（寫 orphans.jsonl）")
    p_oexp.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p_oexp.add_argument("--timeout", type=float, default=30.0)
    p_oexp.add_argument(
        "--supplement",
        help="REST 取不到的孤兒（如含 NUL 者）從 SurrealDB 唯讀副本手動匯出的 JSONL",
    )

    p_est = sub.add_parser("estimate", help="估算待補摘要／向量（不呼叫任何 API）")
    p_est.add_argument("--db", required=True)
    p_est.add_argument("--config", help="設定檔（讀 rate_per_minute）")
    p_est.add_argument("--summary-seconds", type=float, default=SAMPLE_SUMMARY_SECONDS)
    p_est.add_argument("--embed-seconds", type=float, default=DEFAULT_EMBED_SECONDS)

    args = parser.parse_args(argv)
    if stdout is None:
        # Windows 主控台預設 cp950，經管線讀取時中文會變亂碼；輸出一律 UTF-8
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
    out = stdout if stdout is not None else sys.stdout
    env = os.environ if environ is None else environ
    try:
        if args.command == "export":
            out_dir = _check_outside_repo(Path(args.out), "匯出目錄")
            client = OnClient(
                base_url=args.base_url,
                getter=getter,
                password=env.get(PASSWORD_ENV) or None,
                timeout=args.timeout,
            )
            manifest = export_on(client, out_dir)
            _print(
                out,
                {
                    "export_dir": str(out_dir),
                    "counts": manifest["counts"],
                    "all_notes_endpoint": manifest["all_notes_endpoint"],
                    "multi_membership": len(manifest["multi_membership"]),
                    "duplicate_edges": len(manifest["duplicate_edges"]),
                },
            )
            return 0
        if args.command == "map":
            export_dir = Path(args.export)
            export = load_export(export_dir)
            target = _check_outside_repo(
                Path(args.out) if args.out else export_dir / DEFAULT_MAPPING_FILE,
                "mapping 檔",
            )
            mapping = build_mapping(
                export, search_roots=[Path(p) for p in args.search_root]
            )
            _write_json(target, mapping)
            _print(
                out,
                {
                    "mapping": str(target),
                    "notebooks": len(mapping["notebooks"]),
                    "needs_review": review_items(mapping),
                    "orphans": len(mapping["orphans"]),
                },
            )
            return 0
        if args.command == "import":
            export_dir = Path(args.export)
            export = load_export(export_dir)
            mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            db_path = _check_outside_repo(Path(args.db), "資料庫")
            report_path = _check_outside_repo(
                Path(args.report) if args.report else export_dir / DEFAULT_REPORT_FILE,
                "報告檔",
            )
            orphans = None
            if args.orphans_map:
                orphan_map = json.loads(
                    Path(args.orphans_map).read_text(encoding="utf-8")
                )
                orphans = OrphanPlan(
                    entries=on_orphans.validate_orphan_map(orphan_map),
                    records=on_orphans.load_orphan_records(export_dir, _sha256_file),
                )
            conn = connect(db_path)
            try:
                report = run_import(
                    conn,
                    export,
                    mapping,
                    allow_unreviewed=args.allow_unreviewed,
                    orphans=orphans,
                    principal=configured_principal(environ=env),
                )
            finally:
                conn.close()
            _write_json(report_path, report)
            summary = report_summary(report)
            summary["report"] = str(report_path)
            _print(out, summary)
            return 0
        if args.command == "orphans-map":
            target = _check_outside_repo(Path(args.out), "孤兒 mapping")
            mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            roots = [
                Path(p) for p in args.projects_dir
            ] or on_orphans.default_transcript_dirs()
            orphan_map = on_orphans.build_orphan_map(
                on_orphans.load_orphan_list(Path(args.orphans)),
                mapping,
                roots,
                exclude=args.exclude,
            )
            _write_json(target, orphan_map)
            stats = on_orphans.orphan_map_stats(orphan_map)
            stats["orphans_map"] = str(target)
            _print(out, stats)
            return 0
        if args.command == "export-orphans":
            out_dir = _check_outside_repo(Path(args.export), "匯出目錄")
            orphan_map = json.loads(Path(args.orphans_map).read_text(encoding="utf-8"))
            ids = {e["id"] for e in on_orphans.validate_orphan_map(orphan_map)}
            supplement = (
                on_orphans.load_supplement(Path(args.supplement), ids)
                if args.supplement
                else None
            )
            client = OnClient(
                base_url=args.base_url,
                getter=getter,
                password=env.get(PASSWORD_ENV) or None,
                timeout=args.timeout,
            )
            manifest = on_orphans.export_orphans(
                client.get,
                orphan_map,
                out_dir,
                quote=_quote_id,
                supplement=supplement,
                write_jsonl=_write_jsonl,
                write_json=_write_json,
                sha256_file=_sha256_file,
            )
            _print(
                out,
                {
                    "export_dir": str(out_dir),
                    "counts": manifest["counts"],
                    "unavailable": manifest["unavailable"],
                },
            )
            return 0
        # estimate
        from lore_vault.config import load_config
        from lore_vault.enrich.clients import SUMMARY_SYSTEM_PROMPT

        config = load_config(args.config)
        conn = connect(Path(args.db))
        try:
            result = estimate_backfill(
                conn,
                summary_rate_per_minute=config.summary.rate_per_minute,
                embed_rate_per_minute=config.embedding.rate_per_minute,
                summary_seconds=args.summary_seconds,
                embed_seconds=args.embed_seconds,
                system_prompt=SUMMARY_SYSTEM_PROMPT,
            )
        finally:
            conn.close()
        _print(out, result)
        return 0
    except (OnImportError, on_orphans.OrphanError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
