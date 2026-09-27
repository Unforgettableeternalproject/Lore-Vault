"""concept id 的配號規則：歷來最大編號 + 1，刪除後號碼永不重用。

**原本的規則是 ``c-{len(concepts):03d}``，那在收斂之後必定撞號。**
收斂（consolidate）會把池子縮短，下一次增量蒸餾的 ``len`` 就落回某個仍存在的號碼，
新記憶於是拿到舊記憶的 id。下游全都以 id 為鍵（注入紀錄、校準回寫、服務端 upsert），
撞號時不報錯，只是把兩條不同的記憶混成一條。

高水位存在 concept 檔旁的 sidecar（``<stem>.id_state.json``），而不是
``pipeline_state.json``：蒸餾是被管線當子行程呼叫的，也會被手動或測試以別的
concept 路徑執行；sidecar 跟著 concept 檔走，換路徑就自然隔離。
配號一律取 ``max(高水位, 檔內現存最大號) + 1``——檔案被手動刪減、sidecar 遺失或
被改小，都不會退回已發過的號碼。

並行：配號與寫檔包在 concept 檔旁的 O_EXCL 鎖裡（``<name>.lock``）。
不能沿用 ``pipeline.lock``：管線持鎖時才呼叫蒸餾，重用會自己擋自己。

只用標準庫，不 import 同目錄其他模組。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ID_PATTERN = re.compile(r"^c-(\d+)$")
# 鎖超過這個年齡視為持有者已死（程序被 kill 時鎖不會自己消失）
LOCK_STALE_SECONDS = 600
LOCK_POLL_SECONDS = 0.05


def format_id(number: int) -> str:
    return f"c-{number:03d}"


def id_number(concept_id: Any) -> int | None:
    """``c-123`` → 123；非此形式（例如舊的雜湊 id）回 None，不參與編號。"""
    match = ID_PATTERN.match(str(concept_id or ""))
    return int(match.group(1)) if match else None


def max_id_number(concepts: Iterable[dict[str, Any]]) -> int:
    """現存最大編號；沒有任何編號形式的 id 時回 -1（下一號即 c-000）。"""
    numbers = [n for n in (id_number(c.get("id")) for c in concepts) if n is not None]
    return max(numbers, default=-1)


def state_path(concept_path: Path) -> Path:
    return concept_path.with_name(f"{concept_path.stem}.id_state.json")


def lock_path(concept_path: Path) -> Path:
    return concept_path.with_name(f"{concept_path.name}.lock")


def load_high_water(concept_path: Path) -> int:
    """sidecar 記錄的歷來最大編號；檔案不存在或損毀回 -1（交給現存最大號兜底）。"""
    try:
        value = json.loads(state_path(concept_path).read_text(encoding="utf-8")).get("max_id")
    except (OSError, ValueError, AttributeError):
        return -1
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def save_high_water(concept_path: Path, number: int) -> None:
    """只往上推，不往下寫：呼叫端傳入較小值時保留既有高水位。"""
    number = max(number, load_high_water(concept_path))
    path = state_path(concept_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"max_id": number}), encoding="utf-8")
    os.replace(tmp, path)


class IdAllocator:
    """從 ``max(高水位, 現存最大號) + 1`` 起連續配號。"""

    def __init__(self, concept_path: Path, existing: Iterable[dict[str, Any]]) -> None:
        self.last = max(load_high_water(concept_path), max_id_number(existing))

    def next(self) -> str:
        self.last += 1
        return format_id(self.last)


@contextmanager
def concept_lock(concept_path: Path, *, timeout: float = 60.0) -> Iterator[None]:
    """concept 檔的獨佔鎖，涵蓋「讀池子 → 配號 → 寫回」整段。"""
    path = lock_path(concept_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue  # 剛好被釋放，重試
            if age > LOCK_STALE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"等不到 concept 鎖：{path}（另一個蒸餾正在寫）") from None
            time.sleep(LOCK_POLL_SECONDS)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": time.time()}, f)
        yield
    finally:
        path.unlink(missing_ok=True)
