"""episode 本地 spool 與推送（T-38／T-39）。

目錄佈局（hook 與 doctor 之間的契約，刻意保持簡單）::

    <spool_dir>/pending/<id>.json    待推送；每筆一檔，暫存檔 + os.replace 原子寫入
    <spool_dir>/rejected/<id>.json   服務回 conflict／invalid（或本地檔損毀），
                                     不再自動重推
    <spool_dir>/push_state.json      最近一次推送結果與退避時間

`<id>` 由 (session_id, prompt_id, turn_index) 雜湊而來：同一輪重寫會覆蓋同一檔，
重播到服務端是冪等的（服務回 duplicate 視同成功）。

每筆內容 `{"format": 1, "spooled_at": ..., "episode": {...}}`（有清理控制字元時另有
`"sanitized": <替換數>`）；`episode` 就是送給
`POST /v1/episodes` 的物件，已含寫入當下凍結的 `machine` 與 `vault`——推送、
重播都原樣送出，不依推送當下的機器或 binding 重算（A7）。

推送失敗一律不拋例外：檔案留在 pending，下次（Stop hook 或 `--push`）再推。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lore_vault.schema.chars import sanitize_value

from .client_env import ClientSettings

# `service`（urllib → http.client、ssl）在推送時才 import：Stop hook 沒東西要推、
# 或推送未設定時不付這筆載入成本

FORMAT_VERSION = 1
PENDING = "pending"
REJECTED = "rejected"
STATE_FILE = "push_state.json"
EPISODES_PATH = "/v1/episodes"

# 服務端逐筆狀態
STATUS_ACCEPTED = "accepted"
STATUS_DUPLICATE = "duplicate"
STATUS_CONFLICT = "conflict"
STATUS_INVALID = "invalid"
# 本地判定：spool 檔讀不回來
STATUS_CORRUPT = "corrupt"

DONE_STATUSES = frozenset({STATUS_ACCEPTED, STATUS_DUPLICATE})
REJECT_STATUSES = frozenset({STATUS_CONFLICT, STATUS_INVALID})

# 服務不可達或拒絕後，Stop hook 在這段時間內不再嘗試（`--push` 不受限）
DEFAULT_BACKOFF_SECONDS = 60.0
# 服務明確回「未開啟 episode 收料」（403 `episode_ingest_disabled`，D13）：
# 這是服務端的設定、不是暫時故障，檔案留在 pending（不移到 rejected、不算損毀），
# 退避拉長到小時級，不讓每次 Stop 都打一次服務。服務開啟後下一輪（或 `--push`）補推
INGEST_DISABLED_CODE = "episode_ingest_disabled"
DISABLED_BACKOFF_SECONDS = 6 * 3600.0
# push_state.json 的 last_error_kind
ERROR_KIND_UNAVAILABLE = "unavailable"
ERROR_KIND_REJECTED = "rejected"
ERROR_KIND_DISABLED = "disabled"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return (
        ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ts.microsecond // 1000:03d}Z"
    )


def spool_id(episode: dict[str, Any]) -> str:
    key = "\x00".join(
        str(episode.get(k)) for k in ("session_id", "prompt_id", "turn_index")
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ── 寫入（凍結 machine／vault）──────────────────────────────────────


def derive_vault(
    repo_root: str | None,
    repo: str | None,
    cache: dict[str, str] | None = None,
) -> str:
    """episode 寫入當下的 vault key。

    有 `repo_root` → `lore_vault.binding.resolve_binding`（git remote 正規化，
    無 remote 時 `folder/<資料夾名>`）；目錄已不存在、git 失敗等任何例外 →
    `folder/<repo>`；連 `repo` 都沒有 → `folder/unknown`。
    `cache` 以 repo_root 為鍵，同一次 hook 執行只跑一次 git。
    """
    from lore_vault.binding import folder_key, resolve_binding

    if repo_root:
        if cache is not None and repo_root in cache:
            return cache[repo_root]
        try:
            key = resolve_binding(repo_root).key
        except Exception:  # noqa: BLE001 — 解析失敗一律退回資料夾名
            key = folder_key(repo or Path(repo_root).name or "unknown")
        if cache is not None:
            cache[repo_root] = key
        return key
    return folder_key(repo or "unknown")


def wire_episode(
    episode: dict[str, Any],
    *,
    machine: str,
    vault: str,
) -> dict[str, Any]:
    """spike episode → 推送格式（多 `machine`、`vault` 兩欄）。不修改原物件。"""
    wire = dict(episode)
    wire["machine"] = machine
    wire["vault"] = vault
    return wire


def write_pending(
    spool_dir: Path, wire: dict[str, Any], *, now: datetime | None = None
) -> Path:
    """寫一筆待推送。NUL 等禁用控制字元先換成可見形式（`\\0`、`\\xNN`），
    替換數記在 `sanitized`（只在非零時出現）；推送只讀 `episode`，這欄不影響協定。"""
    wire, replaced = sanitize_value(wire)
    record: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "spooled_at": _iso(now or _utc_now()),
        "episode": wire,
    }
    if replaced:
        record["sanitized"] = replaced
    path = spool_dir / PENDING / f"{spool_id(wire)}.json"
    _atomic_write_json(path, record)
    return path


def spool_episodes(
    spool_dir: Path,
    episodes: Iterable[dict[str, Any]],
    *,
    machine: str,
    vault_for: Callable[[dict[str, Any]], str],
    now: datetime | None = None,
) -> int:
    count = 0
    for episode in episodes:
        wire = wire_episode(episode, machine=machine, vault=vault_for(episode))
        write_pending(spool_dir, wire, now=now)
        count += 1
    return count


# ── 讀取與統計 ─────────────────────────────────────────────────────


def _entries(directory: Path) -> list[tuple[float, Path]]:
    try:
        it = os.scandir(directory)
    except FileNotFoundError:
        return []
    result: list[tuple[float, Path]] = []
    with it:
        for entry in it:
            if entry.name.endswith(".json") and not entry.name.startswith("."):
                try:
                    result.append((entry.stat().st_mtime, Path(entry.path)))
                except FileNotFoundError:
                    continue  # 另一個程序剛推完刪掉
    result.sort()
    return result


@dataclass(frozen=True)
class SpoolStats:
    pending: int
    oldest_pending_age: float | None  # 秒；沒有待推送時 None
    rejected: int
    rejected_by_status: dict[str, int] = field(default_factory=dict)


def spool_stats(spool_dir: Path, *, now: datetime | None = None) -> SpoolStats:
    now_ts = (now or _utc_now()).timestamp()
    pending = _entries(spool_dir / PENDING)
    oldest = max(0.0, now_ts - pending[0][0]) if pending else None
    by_status: dict[str, int] = {}
    rejected = _entries(spool_dir / REJECTED)
    for _, path in rejected:
        try:
            status = json.loads(path.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError, AttributeError):
            status = None
        name = status if isinstance(status, str) else STATUS_CORRUPT
        by_status[name] = by_status.get(name, 0) + 1
    return SpoolStats(len(pending), oldest, len(rejected), by_status)


def load_push_state(spool_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((spool_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── 推送 ───────────────────────────────────────────────────────────


@dataclass
class PushResult:
    attempted: bool = False
    sent: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    kept: int = 0
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def done(self) -> int:
        return self.accepted + self.duplicate

    def summary(self) -> str:
        if self.skipped_reason:
            return f"未推送：{self.skipped_reason}"
        text = (
            f"送出 {self.sent}：accepted {self.accepted}、duplicate {self.duplicate}、"
            f"rejected {self.rejected}、留待重推 {self.kept}"
        )
        return text + (f"（{self.error}）" if self.error else "")


def _reject(
    path: Path, spool_dir: Path, status: str, detail: str | None, now: datetime
) -> None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError
    except (OSError, ValueError):
        record = {"format": FORMAT_VERSION, "episode": None}
    record["status"] = status
    record["rejected_at"] = _iso(now)
    if detail:
        record["detail"] = str(detail)[:500]
    _atomic_write_json(spool_dir / REJECTED / path.name, record)
    path.unlink(missing_ok=True)


def _result_items(response: Any, expected: int) -> list[dict[str, Any]] | None:
    """回應形狀：`{"results": [{"status": ...}, ...]}`，與請求逐筆同序。
    形狀不符回 None（整批留在 spool，下次重推最壞只是 duplicate）。"""
    if not isinstance(response, dict):
        return None
    items = response.get("results")
    if not isinstance(items, list) or len(items) != expected:
        return None
    if not all(isinstance(i, dict) and isinstance(i.get("status"), str) for i in items):
        return None
    return items


def _save_state(spool_dir: Path, state: dict[str, Any]) -> None:
    try:
        _atomic_write_json(spool_dir / STATE_FILE, state)
    except OSError:
        pass


def push_pending(
    spool_dir: Path,
    settings: ClientSettings,
    *,
    limit: int | None = None,
    timeout: float | None = None,
    respect_backoff: bool = True,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    now: Callable[[], datetime] = _utc_now,
) -> PushResult:
    """推一批（最舊的 `limit` 筆）。任何失敗都不拋例外，檔案留在 pending。

    服務回 `episode_ingest_disabled` 時記 `last_error_kind = "disabled"`、退避
    `DISABLED_BACKOFF_SECONDS`（`--push` 不受退避限制，但同樣只推一次就停）。"""
    from .service import ServiceError, ServiceUnavailable, error_code, request_json

    result = PushResult()
    if not settings.push_configured:
        result.skipped_reason = settings.describe()
        return result
    state = load_push_state(spool_dir)
    started = now()
    if respect_backoff:
        until = state.get("backoff_until_ts")
        if isinstance(until, (int, float)) and started.timestamp() < until:
            result.skipped_reason = "退避中（上次推送失敗）"
            return result

    batch = _entries(spool_dir / PENDING)[: limit or settings.push_batch]
    if not batch:
        result.skipped_reason = "沒有待推送"
        return result

    paths: list[Path] = []
    episodes: list[dict[str, Any]] = []
    for _, path in batch:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            episode = record["episode"]
            if not isinstance(episode, dict):
                raise TypeError
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError):
            _reject(path, spool_dir, STATUS_CORRUPT, "spool 檔損毀", started)
            result.rejected += 1
            continue
        paths.append(path)
        episodes.append(episode)
    if not episodes:
        return result

    result.attempted = True
    result.sent = len(episodes)
    state["last_attempt_at"] = _iso(started)
    try:
        response = request_json(
            settings,
            "POST",
            EPISODES_PATH,
            {"episodes": episodes},
            timeout=timeout if timeout is not None else settings.push_timeout,
        )
        items = _result_items(response, len(episodes))
        if items is None:
            raise ServiceError("回應格式不符（缺 results 或筆數不一致）")
    except ServiceError as exc:
        disabled = error_code(exc.body) == INGEST_DISABLED_CODE
        result.kept = len(episodes)
        result.error = exc.detail
        if disabled:
            result.error = "服務未開啟 episode 收料，紀錄留在本機、開啟後自動補推"
        state["last_error"] = result.error
        if disabled:
            state["last_error_kind"] = ERROR_KIND_DISABLED
        elif isinstance(exc, ServiceUnavailable):
            state["last_error_kind"] = ERROR_KIND_UNAVAILABLE
        else:
            state["last_error_kind"] = ERROR_KIND_REJECTED
        delay = DISABLED_BACKOFF_SECONDS if disabled else backoff_seconds
        state["backoff_until_ts"] = started.timestamp() + delay
        _save_state(spool_dir, state)
        return result

    finished = now()
    for path, item in zip(paths, items, strict=True):
        status = item["status"]
        if status in DONE_STATUSES:
            path.unlink(missing_ok=True)
            if status == STATUS_ACCEPTED:
                result.accepted += 1
            else:
                result.duplicate += 1
        elif status in REJECT_STATUSES:
            detail = item.get("detail") or item.get("message") or item.get("error")
            _reject(path, spool_dir, status, detail, finished)
            result.rejected += 1
        else:
            result.kept += 1
    state.pop("backoff_until_ts", None)
    state.pop("last_error", None)
    state.pop("last_error_kind", None)
    state["last_ok_at"] = _iso(finished)
    _save_state(spool_dir, state)
    return result


def push_all(
    spool_dir: Path,
    settings: ClientSettings,
    *,
    max_batches: int = 1000,
    timeout: float | None = None,
) -> PushResult:
    """手動／排程用：一批接一批推到空、失敗或整批無進展為止。忽略退避。"""
    total = PushResult()
    for index in range(max_batches):
        res = push_pending(spool_dir, settings, timeout=timeout, respect_backoff=False)
        if index == 0 and res.skipped_reason:
            total.skipped_reason = res.skipped_reason
        for name in ("sent", "accepted", "duplicate", "rejected", "kept"):
            setattr(total, name, getattr(total, name) + getattr(res, name))
        total.attempted = total.attempted or res.attempted
        if res.error:
            total.error = res.error
            break
        if res.done + res.rejected == 0:
            break
    if total.attempted or total.rejected:
        total.skipped_reason = None
    return total
