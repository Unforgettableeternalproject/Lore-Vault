"""pdf／docx／pptx 抽取的子行程隔離：逾時與記憶體上限。

病態檔案（例如讓 pypdf 陷入超長迴圈的 pdf）不能卡死整個文件佇列：抽取在獨立
子行程執行，超過 `timeout` 秒直接 kill，視為 `corrupt`（detail 註明 timeout）。

- 一律用 `spawn`：服務的 worker 在背景執行緒裡跑，fork 有鎖狀態複製的風險；
  Python 3.14 在 Linux 的預設 start method 也已不是 fork。Windows 只有 spawn。
- 逾時從子行程回報「就緒」（已設好記憶體上限）之後起算，不含直譯器啟動與
  import 的時間（機器忙碌時 spawn 可能要數秒）；就緒本身另有 `STARTUP_TIMEOUT`。
- 記憶體上限：`memory_bytes > 0` 時子行程以 `resource.setrlimit(RLIMIT_AS)`
  限制虛擬位址空間（Linux 容器）。超過時解析套件拋 `MemoryError`，`extract()` 會
  轉成 `too_large`；被系統直接殺掉則視為異常結束。Windows 沒有 `resource` 模組，
  不設上限（只有逾時保護）。
- 子行程只回傳 tuple：`ExtractionError` 的 `args` 是格式化字串，直接 pickle
  例外物件回來會在 unpickle 時 TypeError。
- 異常結束（被 OOM killer 殺掉、segfault、沒回結果就退出）不是決定性失敗，
  拋 `IsolatedExtractionFailed`，由 worker 的有上限重試處理。
- `should_stop()` 為真時中止子行程並拋 `ExtractionInterrupted`（服務關閉），
  文件留在 extracting，下次啟動由 `recover_interrupted` 收回 pending。

被執行的函式與參數必須可 pickle（模組頂層函式）。本模組只 import
`documents.extract`，子行程不會載入 config／api。
"""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable
from typing import Any

from .extract import CORRUPT, TOO_LARGE, ExtractionError

# 子行程啟動到就緒（直譯器啟動、import、設定上限）的時限
STARTUP_TIMEOUT = 120.0
# 等待結果時每次 poll 的時間片（期間檢查 should_stop 與子行程是否還活著）
_POLL_SLICE = 0.2
# kill 之後等待子行程結束的時限
_REAP_TIMEOUT = 10.0

_CTX = multiprocessing.get_context("spawn")


class IsolatedExtractionFailed(RuntimeError):
    """子行程異常結束或拋出非預期例外（非決定性，交給重試）。"""


class ExtractionInterrupted(Exception):
    """`should_stop()` 為真，抽取被中止（服務關閉）。"""


def _limit_memory(memory_bytes: int) -> None:
    if memory_bytes <= 0:
        return
    try:
        import resource
    except ImportError:  # Windows：沒有 RLIMIT_AS，只靠逾時
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError):  # 已有更低的硬上限等：沿用既有上限
        pass


def _child_main(
    conn: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    memory_bytes: int,
) -> None:
    try:
        _limit_memory(memory_bytes)
        conn.send(("ready",))
        try:
            result = func(*args, **kwargs)
        except ExtractionError as exc:
            conn.send(("error", exc.code, exc.detail))
            return
        except MemoryError:
            conn.send(("error", TOO_LARGE, "抽取時記憶體不足（超過子行程上限）"))
            return
        except Exception as exc:  # noqa: BLE001 - 原因帶回父行程，由重試處理
            conn.send(("exception", f"{type(exc).__name__}: {exc}"))
            return
        conn.send(("ok", result))
    finally:
        conn.close()


_TIMEOUT = object()
_DIED = object()


def _wait(
    conn: Any,
    proc: Any,
    seconds: float,
    should_stop: Callable[[], bool],
) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _TIMEOUT
        if conn.poll(min(_POLL_SLICE, remaining)):
            try:
                return conn.recv()
            except EOFError:  # 子行程沒送結果就關了管線（結束）
                return _DIED
        if should_stop():
            raise ExtractionInterrupted("服務關閉，抽取中止")
        if not proc.is_alive() and not conn.poll():
            return _DIED


def run_isolated(
    func: Callable[..., Any],
    *args: Any,
    timeout: float,
    memory_bytes: int = 0,
    should_stop: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> Any:
    """在子行程執行 `func(*args, **kwargs)`，回傳其結果。

    - `func` 拋 `ExtractionError` → 原樣（code／detail）在父行程重拋
    - 超過 `timeout` 秒 → kill 子行程，拋 `ExtractionError(corrupt, "...timeout...")`
    - 子行程異常結束、未就緒、或拋其他例外 → `IsolatedExtractionFailed`
    - `should_stop()` 為真 → kill 子行程，拋 `ExtractionInterrupted`
    """
    if timeout <= 0:
        raise ValueError("timeout 必須大於 0")
    stop = should_stop or (lambda: False)
    receiver, sender = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(
        target=_child_main,
        args=(sender, func, args, kwargs, memory_bytes),
        name="lore-vault-extract",
        daemon=True,
    )
    proc.start()
    # 父行程只留讀端：子行程結束時寫端全部關閉，recv 才會拿到 EOF
    sender.close()
    try:
        message = _wait(receiver, proc, STARTUP_TIMEOUT, stop)
        if message is _TIMEOUT:
            raise IsolatedExtractionFailed(f"抽取子行程 {STARTUP_TIMEOUT:g} 秒內未就緒")
        if message is _DIED or message[0] != "ready":
            raise IsolatedExtractionFailed(_died(proc, "啟動"))
        message = _wait(receiver, proc, timeout, stop)
        if message is _TIMEOUT:
            raise ExtractionError(
                CORRUPT,
                f"抽取逾時（timeout：超過 {timeout:g} 秒），已中止子行程；"
                "檔案可能使解析套件陷入超長處理",
            )
        if message is _DIED:
            raise IsolatedExtractionFailed(_died(proc, "抽取"))
        kind = message[0]
        if kind == "ok":
            return message[1]
        if kind == "error":
            raise ExtractionError(message[1], message[2])
        raise IsolatedExtractionFailed(f"抽取子行程例外：{message[1]}")
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(_REAP_TIMEOUT)
        receiver.close()


def _died(proc: Any, stage: str) -> str:
    proc.join(_REAP_TIMEOUT)
    return f"抽取子行程在{stage}階段異常結束（exit code {proc.exitcode}）"
