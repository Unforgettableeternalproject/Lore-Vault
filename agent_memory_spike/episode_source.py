"""D13：管線的 episode 來源——從服務拉取全部機器的 episode，與本機 jsonl 合併去重。

## 佈局（``paths.EPISODE_CACHE_DIR``）

    service.jsonl           從服務拉到的 episode，一行一筆（Episode dict + ``vault`` + ``seq``），只追加
    state.json              水位與對帳狀態（``after_seq``、``server_missing`` 等）
    merged/episodes.jsonl   每輪重建：本機 jsonl ∪ service.jsonl，依 spool 鍵去重；蒸餾讀這裡

水位放在快取目錄裡：整個目錄刪掉＝重置，下次自動全量重拉。

## 水位用服務端 ``seq``

- ``started_at``（對話時間）不行：遠端機器離線幾天後補推，舊對話晚到貨，
  水位早就越過它的 ``started_at``，會被永久漏掉。
- ``recorded``（收料時間）也不理想：同一毫秒可有多筆，且依賴服務端時鐘單調。
- ``seq`` 是 INTEGER PRIMARY KEY，episodes 只插入不刪改，SQLite 單一寫者讓 seq 依序可見，
  ``seq > 水位`` 不漏也不重。服務端每頁回 ``max_seq`` 與 ``total``（seq <= max_seq 的筆數），
  兩者都在同一個 max_seq 邊界下計算。

保護（任何一項觸發就改為全量重拉，併入既有快取、不丟任何已拉到的資料）：

1. 沒有水位（初次、或快取被刪）
2. 服務端 ``max_seq`` 小於水位：資料庫被還原或重建
3. 快取中「服務端仍有」的筆數 ≠ ``total``：有列落在水位之前卻沒拉到（seq 重用等），
   或服務端少了資料
4. 舊版服務不認得 ``after_seq``（回應沒有 ``max_seq``）：退回 cursor 全量，不設水位

全量重拉時，快取有而服務端已沒有的鍵記為 ``server_missing``——那些是服務端曾經收下、
現在卻不見的 episode（本機 spool 推送成功後被刪檔，這是它們唯一的對帳點），doctor 報 fail。

## 去重鍵與合併規則

鍵是 spool 鍵 ``(session_id, prompt_id, turn_index)``（服務端的唯一鍵，``spool.spool_id`` 同源）。

- 本機與服務都有：**取本機那份**，另掛服務端凍結的 ``vault``／``machine``。
  本機是原始語料（未經控制字元清理、可能被 ``--repair`` 補完整），蒸餾對本機 episode 的行為
  與改動前逐字相同
- 只在服務端：照收；若 ``machine`` 不是本機，``repo_root`` 清成 None（A13 註：repo_root 只在
  同一台機器上有意義，跨機器歸屬靠 vault）
- 只在本機（尚未推送、或 spool 之前的歷史語料）：照收

跨 session 的 resume 複本（同一輪、不同 session_id）不在這裡處理，交給蒸餾端既有的
``hook_stop.load_deduped``（以 prompt_id + user_text 指紋 + 前綴關係判定）。

## 蒸餾中依賴本機路徑的部分

蒸餾（``distill.py``）不讀 ``repo_root``／``cwd``，也不碰檔案系統；檔案重疊用
``transcript.file_key``（取路徑末段、統一斜線、小寫）做字串比對，與作業系統無關，
遠端的 ``files_edited`` 照樣可比。產出 concept 的 ``scope`` 取 episode 的 ``repo``（名稱，不是路徑），
``source_files`` 是原始路徑（溯源用，PreToolUse 退回用時也走 file_key 末段比對）。
所以唯一只在本機有意義的欄位是 ``repo_root``，對遠端 episode 清掉，避免之後有程式在本機解析它。

本模組只用標準庫與 ``lore_vault.hooks``（``service``／``spool``）；不 import 同目錄模組。
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
SERVICE_LOG = "service.jsonl"
CACHE_STATE = "state.json"
MERGED_DIR = "merged"
MERGED_FILE = "episodes.jsonl"

PAGE_SIZE = 1000  # 服務端上限（spike.EPISODE_PAGE_MAX）
MAX_PAGES = 10_000
SERVICE_TIMEOUT = 60.0
MISSING_SAMPLE = 20

# pipeline_state.json 裡的拉取紀錄。doctor（lore_vault.doctor.episode_pull_check）與
# 健康告警讀同一個鍵與同一組值，改名要一起改（test_episode_source 驗證）
EPISODE_PULL_KEY = "episode_pull"
MODE_SERVICE = "service"          # 從服務拉取成功
MODE_FALLBACK = "local_fallback"  # 拉取失敗，退回本機 jsonl ＋ 上次的快取（會漏新的遠端 episode，但不會錯）
MODE_FORCED = "local_forced"      # --episode-source local：只讀本機 jsonl（除錯用，等同改動前）
MODE_FAILED = "failed"            # 拉取失敗且 --on-pull-failure fail

ERROR_UNCONFIGURED = "unconfigured"
ERROR_UNAVAILABLE = "unavailable"
ERROR_REJECTED = "rejected"
ERROR_CONSISTENCY = "consistency"

Key = tuple[str, str, int]


class PullError(RuntimeError):
    """拉取失敗。``kind`` 是上面的 ERROR_*。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _LegacyServer(Exception):
    """服務端不認得 after_seq（回應沒有 max_seq）。"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_machine() -> str:
    """與 Stop hook 凍結 ``machine`` 的方式相同（``hook_stop.current_machine``）。"""
    return platform.node() or "unknown"


def episode_key(rec: dict[str, Any]) -> Key:
    return (str(rec.get("session_id")), str(rec.get("prompt_id")),
            int(rec.get("turn_index") or 0))


def _key_list(key: Key) -> list[Any]:
    return [key[0], key[1], key[2]]


# --- 快取讀寫 -----------------------------------------------------------------


def load_cache_state(cache_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((cache_dir / CACHE_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_cache_state(cache_dir: Path, state: dict[str, Any]) -> None:
    _atomic_write(cache_dir / CACHE_STATE, json.dumps(state, ensure_ascii=False, indent=2))


def load_service_log(cache_dir: Path) -> dict[Key, dict[str, Any]]:
    """讀 service.jsonl。同鍵多行（追加後、寫 state 前中斷再重拉）取最後一行。
    壞行略過：追加寫入中斷只可能壞最後一行，而那一筆下次會從水位後重拉。"""
    records: dict[Key, dict[str, Any]] = {}
    path = cache_dir / SERVICE_LOG
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records[episode_key(rec)] = rec
    return records


def _append_service_log(cache_dir: Path, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / SERVICE_LOG).open("a", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --- 服務端讀取 ----------------------------------------------------------------


def _check_items(page: Any, *, need_seq: bool) -> list[dict[str, Any]]:
    from lore_vault.hooks.service import ServiceRejected

    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ServiceRejected("GET /v1/episodes 回應格式不符（缺 items）")
    items = page["items"]
    for item in items:
        if (not isinstance(item, dict) or not item.get("session_id")
                or item.get("prompt_id") is None or not isinstance(item.get("vault"), str)
                or (need_seq and (isinstance(item.get("seq"), bool)
                                  or not isinstance(item.get("seq"), int)))):
            raise ServiceRejected("GET /v1/episodes 回應的 episode 缺鍵、vault 或 seq")
    return items


def fetch_after(settings, after_seq: int, *, page_size: int = PAGE_SIZE,  # noqa: ANN001
                timeout: float = SERVICE_TIMEOUT,
                max_pages: int = MAX_PAGES) -> tuple[list[dict[str, Any]], int, int]:
    """增量讀到底，回傳 (items, max_seq, total)；max_seq／total 取最後一頁的值
    （每頁都以當下的 max_seq 為邊界，最後一頁的邊界涵蓋先前各頁）。"""
    from lore_vault.hooks.service import ServiceRejected, request_json

    items: list[dict[str, Any]] = []
    after = after_seq
    for _ in range(max_pages):
        page = request_json(settings, "GET", "/v1/episodes", timeout=timeout,
                            query={"vault": "*", "limit": str(page_size),
                                   "after_seq": str(after)})
        if isinstance(page, dict) and "max_seq" not in page:
            raise _LegacyServer
        items.extend(_check_items(page, need_seq=True))
        max_seq, total = page.get("max_seq"), page.get("total")
        if not isinstance(max_seq, int) or not isinstance(total, int):
            raise ServiceRejected("GET /v1/episodes 回應的 max_seq／total 不是整數")
        nxt = page.get("next_after_seq")
        if nxt is None:
            return items, max_seq, total
        if not isinstance(nxt, int) or nxt <= after:
            raise ServiceRejected("GET /v1/episodes 的 next_after_seq 沒有前進")
        after = nxt
    raise PullError(ERROR_REJECTED, f"episode 分頁超過 {max_pages} 頁仍未讀完")


def fetch_legacy(settings, *, page_size: int = PAGE_SIZE,  # noqa: ANN001
                 timeout: float = SERVICE_TIMEOUT,
                 max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    """舊版服務：cursor 全量（依對話時間排序，不能當水位）。"""
    from lore_vault.hooks.service import request_json

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        query = {"vault": "*", "limit": str(page_size)}
        if cursor:
            query["cursor"] = cursor
        page = request_json(settings, "GET", "/v1/episodes", timeout=timeout, query=query)
        items.extend(_check_items(page, need_seq=False))
        cursor = page.get("next_cursor")
        if not cursor:
            return items
    raise PullError(ERROR_REJECTED, f"episode 分頁超過 {max_pages} 頁仍未讀完")


# --- 拉取 ----------------------------------------------------------------------


@dataclass
class PullResult:
    fetched: int = 0            # 本輪從服務讀到的筆數
    appended: int = 0           # 本輪新寫進快取的筆數
    cached: int = 0             # 快取中不重複的鍵數
    service_total: int | None = None  # 服務端 total（舊版服務為 None）
    after_seq: int | None = None
    resync_reason: str | None = None
    legacy: bool = False
    server_missing: list[list[Any]] = field(default_factory=list)


def _changed(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    if old is None:
        return True
    strip = ("seq",)
    return ({k: v for k, v in old.items() if k not in strip}
            != {k: v for k, v in new.items() if k not in strip})


def count_mismatch(present: int, total: int) -> str | None:
    """保護 3：快取中「服務端仍有」的筆數必須等於服務端 total，否則回傳全量重拉的理由。

    水位之前若有列沒拉到（seq 重用、服務端資料被換掉），增量讀取永遠看不到它們；
    只有筆數對帳抓得到。"""
    if present == total:
        return None
    return f"快取中服務端仍有的 {present} 筆與服務端 {total} 筆不符"


def pull(settings, cache_dir: Path, *, page_size: int = PAGE_SIZE,  # noqa: ANN001
         timeout: float = SERVICE_TIMEOUT) -> PullResult:
    """增量拉取到快取。失敗拋 ``PullError``（或 ServiceError，由呼叫端分類）。

    寫入順序：先追加 service.jsonl，再寫 state.json（水位）。中間中斷只會讓下次從舊水位
    重拉、重複的鍵不再追加——不會跳過資料。"""
    state = load_cache_state(cache_dir)
    log = load_service_log(cache_dir)
    result = PullResult()
    watermark = state.get("after_seq")
    if isinstance(watermark, bool) or not isinstance(watermark, int):
        watermark = None
    previous_missing = {tuple(k) for k in state.get("server_missing") or []
                        if isinstance(k, list) and len(k) == 3}

    def absorb(items: list[dict[str, Any]]) -> None:
        fresh = [i for i in items if _changed(log.get(episode_key(i)), i)]
        _append_service_log(cache_dir, fresh)
        for item in fresh:
            log[episode_key(item)] = item
        result.fetched += len(items)
        result.appended += len(fresh)

    try:
        if watermark is None:
            result.resync_reason = "沒有水位（初次拉取或快取被重置）"
        else:
            items, max_seq, total = fetch_after(settings, watermark, page_size=page_size,
                                                timeout=timeout)
            if max_seq < watermark:
                result.resync_reason = (f"服務端 max_seq {max_seq} 小於水位 {watermark}"
                                        "（資料庫可能被還原或重建）")
            else:
                absorb(items)
                result.resync_reason = count_mismatch(len(set(log) - previous_missing), total)
                if result.resync_reason is None:
                    result.after_seq, result.service_total = max_seq, total
                    result.server_missing = [list(k) for k in sorted(previous_missing)]

        if result.resync_reason is not None:
            items, max_seq, total = fetch_after(settings, 0, page_size=page_size,
                                                timeout=timeout)
            absorb(items)
            on_server = {episode_key(i) for i in items}
            if len(on_server) != total:
                # 同一個 max_seq 邊界下讀完全部，筆數仍對不上＝服務端回應自相矛盾
                raise PullError(ERROR_CONSISTENCY,
                                f"全量拉取 {len(on_server)} 筆與服務端 total {total} 不符")
            result.after_seq, result.service_total = max_seq, total
            result.server_missing = [_key_list(k) for k in sorted(set(log) - on_server)]
    except _LegacyServer:
        items = fetch_legacy(settings, page_size=page_size, timeout=timeout)
        absorb(items)
        on_server = {episode_key(i) for i in items}
        result.legacy = True
        result.resync_reason = "服務端不支援 after_seq（舊版），改 cursor 全量拉取"
        result.after_seq = None
        result.server_missing = [_key_list(k) for k in sorted(set(log) - on_server)]
    except PullError:
        # 保留已追加的資料，但水位清掉：下次一定全量重拉重新對帳
        save_cache_state(cache_dir, {**state, "format": FORMAT_VERSION, "after_seq": None,
                                     "failed_at": utc_now()})
        raise

    result.cached = len(log)
    save_cache_state(cache_dir, {
        "format": FORMAT_VERSION,
        "after_seq": result.after_seq,
        "service_total": result.service_total,
        "legacy": result.legacy,
        "server_missing": result.server_missing,
        "pulled_at": utc_now(),
    })
    return result


# --- 合併 ----------------------------------------------------------------------


def load_local(episode_dir: Path) -> dict[Key, dict[str, Any]]:
    """本機 jsonl（每 session 一檔）。同鍵取最後一筆（``--repair`` 重寫後的版本在後面）。"""
    records: dict[Key, dict[str, Any]] = {}
    files = sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records[episode_key(rec)] = rec
    return records


def spooled_ids(spool_dir: Path | None) -> set[str]:
    """spool 中待推送／被拒收的 spool_id（檔名即 id）。"""
    ids: set[str] = set()
    if spool_dir is None:
        return ids
    for sub in ("pending", "rejected"):
        directory = spool_dir / sub
        if directory.is_dir():
            ids.update(p.stem for p in directory.glob("*.json"))
    return ids


def merge(local: dict[Key, dict[str, Any]], service: dict[Key, dict[str, Any]], *,
          machine: str, spool_dir: Path | None = None) -> tuple[list[dict[str, Any]],
                                                              dict[str, int]]:
    """本機 ∪ 服務，依 spool 鍵去重。規則見模組說明。回傳 (合併結果, 計數)。"""
    from lore_vault.hooks.spool import spool_id

    merged: list[dict[str, Any]] = []
    counts = {"local": len(local), "service": len(service), "both": 0, "local_only": 0,
              "local_only_spooled": 0, "service_only": 0, "foreign": 0}
    spooled = spooled_ids(spool_dir)
    for key, rec in local.items():
        out = dict(rec)
        srv = service.get(key)
        if srv is not None:
            out["vault"] = srv.get("vault")
            out["machine"] = srv.get("machine")
            counts["both"] += 1
        else:
            counts["local_only"] += 1
            if spool_id(rec) in spooled:
                counts["local_only_spooled"] += 1
        merged.append(out)
    for key, srv in service.items():
        if key in local:
            continue
        out = {k: v for k, v in srv.items() if k != "seq"}
        if out.get("machine") != machine:
            # 別台機器的絕對路徑在這裡沒有意義（A13 註），不讓任何下游拿去解析
            out["repo_root"] = None
            counts["foreign"] += 1
        counts["service_only"] += 1
        merged.append(out)
    merged.sort(key=lambda e: (str(e.get("session_id")), int(e.get("turn_index") or 0)))
    return merged, counts


def write_merged(cache_dir: Path, episodes: list[dict[str, Any]]) -> Path:
    """寫 merged/episodes.jsonl（暫存檔 + os.replace）。目錄內只留這一個 jsonl。"""
    directory = cache_dir / MERGED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MERGED_FILE
    _atomic_write(target, "".join(json.dumps(e, ensure_ascii=False) + "\n"
                                  for e in episodes))
    for stray in directory.glob("*.jsonl"):
        if stray != target:
            stray.unlink(missing_ok=True)
    return directory


# --- 整體：決定這一輪蒸餾讀哪裡 --------------------------------------------------


@dataclass
class SourceOutcome:
    ok: bool                  # 管線可以往下走（有可用的來源）
    episode_dir: Path | None  # 交給 distill --episode-dir
    record: dict[str, Any]    # 寫進 pipeline_state.json 的 episode_pull
    summary: str


def _classify(exc: BaseException) -> str:
    from lore_vault.hooks.service import ServiceUnavailable

    if isinstance(exc, PullError):
        return exc.kind
    if isinstance(exc, ServiceUnavailable):
        return ERROR_UNAVAILABLE
    return ERROR_REJECTED


def resolve_source(*, source: str, on_failure: str, local_dir: Path, cache_dir: Path,
                   spool_dir: Path | None, settings_loader, previous: dict[str, Any] | None,  # noqa: ANN001
                   machine: str | None = None, page_size: int = PAGE_SIZE,
                   timeout: float = SERVICE_TIMEOUT) -> SourceOutcome:
    """拉取 → 合併 → 回傳這一輪的 episode 目錄與要記錄的狀態。

    ``settings_loader()`` 回傳 ClientSettings；推送未設定時拋 RuntimeError（同 pipeline.service_settings）。
    ``previous`` 是上一筆 episode_pull 紀錄，用來延續 ``last_ok_at``。"""
    machine = machine or local_machine()
    now = utc_now()
    last_ok = (previous or {}).get("last_ok_at")
    record: dict[str, Any] = {"at": now, "last_ok_at": last_ok}

    if source == "local":
        record.update(ok=True, mode=MODE_FORCED, reason="--episode-source local（只讀本機 jsonl）",
                      counts={"local": len(load_local(local_dir))})
        return SourceOutcome(True, local_dir, record,
                             f"強制本機來源 {local_dir}（遠端 episode 不會進蒸餾）")

    pulled: PullResult | None = None
    error: tuple[str, str] | None = None
    try:
        settings = settings_loader()
    except RuntimeError as exc:
        error = (ERROR_UNCONFIGURED, str(exc))
    else:
        try:
            pulled = pull(settings, cache_dir, page_size=page_size, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — 任何拉取失敗都走降級判斷，不讓管線無紀錄地炸掉
            error = (_classify(exc), f"{type(exc).__name__}: {exc}")

    if error is not None and on_failure == "fail":
        record.update(ok=False, mode=MODE_FAILED, error_kind=error[0], reason=error[1][:300])
        return SourceOutcome(False, None, record, f"從服務拉取 episode 失敗：{error[1][:200]}")

    local = load_local(local_dir)
    service = load_service_log(cache_dir)
    merged, counts = merge(local, service, machine=machine, spool_dir=spool_dir)
    episode_dir = write_merged(cache_dir, merged)
    counts["merged"] = len(merged)

    if error is not None:
        record.update(ok=False, mode=MODE_FALLBACK, error_kind=error[0], reason=error[1][:300],
                      counts=counts)
        stale = f"、沿用上次快取 {counts['service']} 筆" if counts["service"] else ""
        return SourceOutcome(True, episode_dir, record,
                             f"⚠ 服務拉取失敗，降級為本機 jsonl{stale}（新的遠端 episode 本輪不會進蒸餾）："
                             f"{error[1][:160]}")

    assert pulled is not None
    counts.update(fetched=pulled.fetched, appended=pulled.appended, cached=pulled.cached,
                  server_missing=len(pulled.server_missing))
    record.update(ok=True, mode=MODE_SERVICE, last_ok_at=now, counts=counts,
                  after_seq=pulled.after_seq, service_total=pulled.service_total,
                  legacy=pulled.legacy, resync_reason=pulled.resync_reason,
                  server_missing_sample=pulled.server_missing[:MISSING_SAMPLE])
    summary = (f"服務 {pulled.fetched} 筆（新增 {pulled.appended}，快取 {pulled.cached}）＋本機 "
               f"{counts['local']} → 合併 {counts['merged']}（重疊 {counts['both']}、"
               f"僅本機 {counts['local_only']}、遠端機器 {counts['foreign']}）")
    if pulled.resync_reason:
        summary += f"｜全量重拉：{pulled.resync_reason}"
    if pulled.server_missing:
        summary += f"｜⚠ 服務端缺少快取中的 {len(pulled.server_missing)} 筆"
    return SourceOutcome(True, episode_dir, record, summary)
