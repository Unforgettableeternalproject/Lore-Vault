"""任務目錄（預設 `openspec/`）：設定解析、`.openspec.yaml` 讀寫、狀態推導與 validate。

目錄結構沿用 OpenSpec：`config.yaml`、`specs/<capability>/spec.md`、
`changes/<name>/{.openspec.yaml, proposal.md, design.md?, tasks.md,
specs/<cap>/spec.md}`、
`changes/archive/<YYYY-MM-DD>-<name>/`。

任務目錄位置：`--root` > 環境變數 `LORE_VAULT_TASKS_ROOT` > 目前目錄下的 `openspec/`
（存在才算）。DECISIONS.md 位置：`--decisions` > `LORE_VAULT_TASKS_DECISIONS` >
`config.yaml` 的 `decisions_file`（相對於任務目錄的上一層）；都沒有就是未設定。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import specs
from .decisions import DECISION_ID, load_decisions

TASKS_ROOT_ENV = "LORE_VAULT_TASKS_ROOT"
DECISIONS_ENV = "LORE_VAULT_TASKS_DECISIONS"
DEFAULT_DIR = "openspec"
META_FILE = ".openspec.yaml"
CONFIG_FILE = "config.yaml"
DECISIONS_KEY = "decisions_file"
SPACE_DEV = "dev"
SUMMARY_KEY = "summary"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ARCHIVED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)$")

# 推導狀態
STATUS_READY = "可開工"
STATUS_BLOCKED = "被擋住"
STATUS_AUTH = "待授權"
STATUS_DONE = "已完成"
STATUS_UNKNOWN = "無法判定"

# 第 4 項新欄位（含預設值）；`source` 無對應卡時省略
REQUIRED_FIELDS = (
    "schema",
    "created",
    "space",
    "blocked_by",
    "depends_on",
    "requires_authorization",
    "note_id",
    "skip_specs",
)


def default_meta(
    created: str,
    *,
    source: str | None = None,
    blocked_by: list[str] | None = None,
    depends_on: list[str] | None = None,
    requires_authorization: bool = False,
    skip_specs: bool = False,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "schema": "spec-driven",
        "created": created,
        "space": SPACE_DEV,
    }
    if source:
        meta["source"] = source
    meta.update(
        {
            "blocked_by": list(blocked_by or []),
            "depends_on": list(depends_on or []),
            "requires_authorization": requires_authorization,
            "note_id": None,
            "skip_specs": skip_specs,
            "base": {},
            "notes": {},
        }
    )
    return meta


def _plain(value: Any) -> Any:
    """pyyaml 會把 `2026-10-08` 讀成 date；一律轉回字串。"""
    if isinstance(value, _dt.date | _dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必須是 mapping")
    return _plain(data)


def atomic_write_text(path: Path, text: str) -> None:
    """先寫暫存檔再 `os.replace`，中途失敗不留半截檔案。保留呼叫端給的行尾。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(
        path, yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False)
    )


@dataclass
class Change:
    name: str
    path: Path
    archived: bool
    meta: dict[str, Any]
    meta_error: str | None = None

    @property
    def meta_path(self) -> Path:
        return self.path / META_FILE

    def save(self) -> None:
        write_yaml(self.meta_path, self.meta)

    def delta_files(self) -> dict[str, Path]:
        """`{capability: specs/<cap>/spec.md}`。"""
        root = self.path / "specs"
        if not root.is_dir():
            return {}
        return {p.parent.name: p for p in sorted(root.glob("*/spec.md")) if p.is_file()}

    def plans(self) -> dict[str, specs.DeltaPlan]:
        return {
            cap: specs.parse_delta(p.read_text(encoding="utf-8-sig"))
            for cap, p in self.delta_files().items()
        }

    def read_file(self, filename: str) -> str:
        """change 目錄下的文字檔（`proposal.md` 等）；不存在回空字串。

        服務端版本化內容（`remote_store.RemoteChange`）覆寫此方法，archive 的
        note 寫入與 `tasks_progress` 因此不綁定本機檔案。"""
        path = self.path / filename
        return path.read_text(encoding="utf-8-sig") if path.is_file() else ""

    def tasks_progress(self) -> tuple[int, int]:
        done = total = 0
        for line in self.read_file("tasks.md").splitlines():
            m = re.match(r"^\s*[-*]\s+\[([ xX])\]", line)
            if m:
                total += 1
                done += m.group(1) != " "
        return done, total

    def archived_at(self) -> str:
        return str(self.meta.get("archived_at") or self.path.name[:10])


def _load_change(path: Path, archived: bool) -> Change:
    name = path.name
    if archived:
        m = _ARCHIVED_DIR.match(name)
        name = m.group(1) if m else name
    meta: dict[str, Any] = {}
    error = None
    meta_path = path / META_FILE
    if not meta_path.is_file():
        error = f"缺少 {META_FILE}"
    else:
        try:
            meta = read_yaml(meta_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            error = f"{META_FILE} 無法解析：{type(exc).__name__}"
    return Change(name, path, archived, meta, error)


@dataclass
class Workspace:
    root: Path
    decisions_path: Path | None = None
    _decisions: dict[str, bool] | None = field(default=None, repr=False)
    _decisions_loaded: bool = field(default=False, repr=False)

    @property
    def changes_dir(self) -> Path:
        return self.root / "changes"

    @property
    def archive_dir(self) -> Path:
        return self.changes_dir / "archive"

    @property
    def specs_dir(self) -> Path:
        return self.root / "specs"

    @property
    def project_root(self) -> Path:
        return self.root.parent

    def main_spec_path(self, capability: str) -> Path:
        return self.specs_dir / capability / "spec.md"

    def read_main_spec(self, capability: str) -> str | None:
        """原樣讀取（`newline=""` 保留 CRLF，寫回時才能沿用原行尾）。"""
        path = self.main_spec_path(capability)
        if not path.is_file():
            return None
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return fh.read()

    def active(self) -> list[Change]:
        if not self.changes_dir.is_dir():
            return []
        return [
            _load_change(p, archived=False)
            for p in sorted(self.changes_dir.iterdir())
            if p.is_dir() and p.name != "archive" and not p.name.startswith(".")
        ]

    def archived(self) -> list[Change]:
        """依 `archived_at`（無則目錄名日期）排序，舊的在前。"""
        if not self.archive_dir.is_dir():
            return []
        found = [
            _load_change(p, archived=True)
            for p in sorted(self.archive_dir.iterdir())
            if p.is_dir() and not p.name.startswith(".")
        ]
        return sorted(found, key=lambda c: (c.archived_at(), c.path.name))

    def find_active(self, name: str) -> Change | None:
        path = self.changes_dir / name
        if name == "archive" or not path.is_dir():
            return None
        return _load_change(path, archived=False)

    def decisions(self) -> dict[str, bool] | None:
        if not self._decisions_loaded:
            self._decisions = load_decisions(self.decisions_path)
            self._decisions_loaded = True
        return self._decisions


def resolve_root(
    arg: str | None, environ: Mapping[str, str] | None = None, cwd: Path | None = None
) -> Path | None:
    env = os.environ if environ is None else environ
    if arg:
        return Path(arg).expanduser().resolve()
    if env.get(TASKS_ROOT_ENV, "").strip():
        return Path(env[TASKS_ROOT_ENV].strip()).expanduser().resolve()
    candidate = (cwd or Path.cwd()) / DEFAULT_DIR
    return candidate.resolve() if candidate.is_dir() else None


def load_workspace(
    root: Path,
    decisions_arg: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Workspace:
    env = os.environ if environ is None else environ
    decisions: Path | None = None
    if decisions_arg:
        decisions = Path(decisions_arg).expanduser()
    elif env.get(DECISIONS_ENV, "").strip():
        decisions = Path(env[DECISIONS_ENV].strip()).expanduser()
    else:
        config = root / CONFIG_FILE
        if config.is_file():
            try:
                value = read_yaml(config).get(DECISIONS_KEY)
            except (OSError, ValueError, yaml.YAMLError):
                value = None
            if isinstance(value, str) and value.strip():
                decisions = Path(value.strip()).expanduser()
                if not decisions.is_absolute():
                    decisions = root.parent / decisions
    return Workspace(root=root, decisions_path=decisions)


# ── 狀態推導 ────────────────────────────────────────────────────────


def derive_status(change: Change, ws: Workspace) -> tuple[str, list[str]]:
    """回傳 (狀態, 原因)。順序：已完成 > 被擋住 > 無法判定 > 待授權 > 可開工。"""
    if change.archived:
        return STATUS_DONE, []
    meta = change.meta
    reasons: list[str] = []
    unknown: list[str] = []
    blocked_by = meta.get("blocked_by") or []
    if blocked_by:
        decisions = ws.decisions()
        for d in blocked_by:
            if decisions is None:
                unknown.append(f"{d}：找不到 DECISIONS.md")
            elif d not in decisions:
                unknown.append(f"{d}：DECISIONS.md 沒有此小節")
            elif not decisions[d]:
                reasons.append(f"{d} 未裁決")
    archived_names = {c.name for c in ws.archived()}
    for dep in meta.get("depends_on") or []:
        if dep not in archived_names:
            reasons.append(f"依賴 {dep} 未封存")
    if reasons:
        return STATUS_BLOCKED, reasons + unknown
    if unknown:
        return STATUS_UNKNOWN, unknown
    if meta.get("requires_authorization"):
        return STATUS_AUTH, ["需艾斯維爾授權（archive 須帶 --authorized-by）"]
    return STATUS_READY, []


# ── validate ────────────────────────────────────────────────────────


def meta_errors(change: Change) -> list[str]:
    if change.meta_error:
        return [change.meta_error]
    meta = change.meta
    errors = [f"{META_FILE} 缺少欄位 {k}" for k in REQUIRED_FIELDS if k not in meta]
    if "space" in meta and meta["space"] != SPACE_DEV:
        errors.append(f"space 目前只支援 {SPACE_DEV}，得到 {meta['space']!r}")
    for key in ("blocked_by", "depends_on"):
        value = meta.get(key)
        if key in meta and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            errors.append(f"{key} 必須是字串清單")
    for d in meta.get("blocked_by") or []:
        if isinstance(d, str) and not DECISION_ID.match(d):
            errors.append(f"blocked_by 的 {d!r} 不是 D 編號（例如 D6）")
    for key in ("requires_authorization", "skip_specs"):
        if key in meta and not isinstance(meta[key], bool):
            errors.append(f"{key} 必須是 true／false")
    for key in ("base", "notes"):
        if meta.get(key) is not None and not isinstance(meta.get(key), dict):
            errors.append(f"{key} 必須是 mapping")
    return errors


def delta_keys(plans: Mapping[str, specs.DeltaPlan]) -> list[str]:
    return [
        specs.requirement_key(cap, name)
        for cap, plan in plans.items()
        for _, name in plan.operations()
    ]


def requirement_overlap(changes: list[Change]) -> dict[str, list[str]]:
    """同一 `capability/requirement` 出現在一個以上 active change 的 delta。"""
    owners: dict[str, list[str]] = {}
    for change in changes:
        if change.archived:
            continue
        for key in dict.fromkeys(delta_keys(change.plans())):
            owners.setdefault(key, []).append(change.name)
    return {k: v for k, v in owners.items() if len(v) > 1}


def _key_cap(key: str) -> str:
    """`<capability>/<requirement>` 鍵的 capability 部分。"""
    return key.split("/", 1)[0]


def current_base(change: Change, ws: Workspace) -> dict[str, str | None]:
    """主 spec 現值的 base（ADDED 記 None 的語意由「不存在」自然得到）。"""
    result: dict[str, str | None] = {}
    for cap, plan in change.plans().items():
        main = ws.read_main_spec(cap)
        for _, name in plan.operations():
            result[specs.requirement_key(cap, name)] = specs.main_block_hash(main, name)
    return result


def base_errors(
    change: Change, ws: Workspace, *, skip: Collection[str] = ()
) -> list[str]:
    """修正 1：主 spec 現值與 `base` 記錄不符即 error（要先 rebase）。
    delta 有、`base` 沒記的 requirement 也是 error——不自動補，否則守衛形同虛設。

    `skip`：已寫回主 spec 的 capability（archive 續跑）；
    其主 spec 已是合併後內容，不比對。"""
    recorded = change.meta.get("base") or {}
    errors = []
    for key, now in current_base(change, ws).items():
        if _key_cap(key) in skip:
            continue
        if key not in recorded:
            errors.append(
                f"{key}：base 未記錄"
                f"（新增 delta 後執行 validate {change.name} --record-base）"
            )
        elif recorded[key] != now:
            errors.append(
                f"{key}：主 spec 已在本 change 建立後變動（base 過時），"
                f"請先 rebase delta 再執行 validate {change.name} --rebase"
            )
    return errors


def record_base(change: Change, ws: Workspace, *, overwrite: bool) -> list[str]:
    """補記（或 `overwrite` 時重記）base；回傳有變動的鍵。"""
    base = dict(change.meta.get("base") or {})
    changed = []
    for key, now in current_base(change, ws).items():
        if overwrite or key not in base:
            if base.get(key, object()) != now:
                changed.append(key)
            base[key] = now
    change.meta["base"] = base
    change.save()
    return changed


def trial_merge(
    change: Change, ws: Workspace, *, skip: Collection[str] = ()
) -> tuple[dict[str, str], list[str]]:
    """delta 併回試算：回傳 ({capability: 新主 spec 全文（沿用原行尾）}, errors)。

    `skip`：已寫回主 spec 的 capability（archive 續跑），不再試算。"""
    merged: dict[str, str] = {}
    errors: list[str] = []
    for cap, plan in change.plans().items():
        if cap in skip:
            continue
        if not NAME_RE.match(cap):
            errors.append(f"capability 名稱 {cap!r} 只能用小寫英數與 -")
            continue
        main = ws.read_main_spec(cap)
        try:
            text = specs.apply_delta(main, plan, cap, change.name)
        except specs.DeltaError as exc:
            errors.extend(f"{cap}：{m}" for m in exc.messages)
            continue
        newline = specs.detect_newline(main) if main is not None else "\n"
        merged[cap] = text.replace("\n", newline) if newline != "\n" else text
    return merged, errors


def validate_change(
    change: Change,
    ws: Workspace,
    active: list[Change],
    *,
    skip: Collection[str] = (),
) -> list[str]:
    """單一 active change 的全部檢查（格式、overlap、base、併回試算）。

    `skip`：已寫回主 spec 的 capability（archive 續跑），
    略過其併回試算、overlap 與 base；其餘 capability 照常檢查。"""
    errors = meta_errors(change)
    if change.meta_error:
        return errors
    if not NAME_RE.match(change.name):
        errors.append("change 名稱只能用小寫英數與 -")
    deltas = change.delta_files()
    if change.meta.get("skip_specs"):
        if deltas:
            errors.append("skip_specs: true 但有 spec delta；兩者擇一")
        return errors
    if not deltas:
        errors.append("沒有 spec delta（無規格的純任務請設 skip_specs: true）")
        return errors
    _, merge_errors = trial_merge(change, ws, skip=skip)
    errors.extend(merge_errors)
    for key, owners in requirement_overlap(active).items():
        if _key_cap(key) in skip:
            continue
        if change.name in owners:
            others = "、".join(o for o in owners if o != change.name)
            errors.append(
                f"{key}：同時被 active change {others} 修改（requirement_overlap）"
            )
    errors.extend(base_errors(change, ws, skip=skip))
    return errors
