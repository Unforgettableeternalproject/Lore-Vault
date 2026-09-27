"""pdf／docx／pptx 抽取的子行程隔離：逾時、異常結束、錯誤回傳、關閉中止。

子行程用 spawn：被執行的函式必須能在子行程 import，所以假抽取器用標準庫函式
（`time.sleep`、`os._exit`），不用測試模組內的函式（importlib 模式下子行程
不一定 import 得到測試模組）。
"""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from lore_vault.documents.extract import (
    CORRUPT,
    UNSUPPORTED_FORMAT,
    Extraction,
    ExtractionError,
    extract,
)
from lore_vault.documents.isolation import (
    ExtractionInterrupted,
    IsolatedExtractionFailed,
    run_isolated,
)


def _no_children():
    return not multiprocessing.active_children()


def test_success_returns_extraction(make_pdf):
    data = make_pdf(["Hello isolated extraction world, " * 3])
    result = run_isolated(extract, data, "a.pdf", timeout=60)
    assert isinstance(result, Extraction) and result.format == "pdf"
    assert "isolated extraction" in result.segments[0].text


def test_extraction_error_is_passed_through():
    with pytest.raises(ExtractionError) as info:
        run_isolated(extract, b"not a pdf at all", "a.pdf", timeout=60)
    assert info.value.code == UNSUPPORTED_FORMAT
    assert "%PDF-" in info.value.detail


def test_timeout_kills_child_and_is_corrupt():
    started = time.monotonic()
    with pytest.raises(ExtractionError) as info:
        run_isolated(time.sleep, 3600, timeout=1.0)
    elapsed = time.monotonic() - started
    assert info.value.code == CORRUPT and "timeout" in info.value.detail
    # 逾時從子行程就緒起算：總耗時 = 啟動 + 1 秒，不會等到 sleep 結束
    assert elapsed < 30
    assert _no_children()


def test_child_exiting_without_result_is_retryable_failure():
    with pytest.raises(IsolatedExtractionFailed) as info:
        run_isolated(os._exit, 3, timeout=30)
    assert "exit code 3" in str(info.value)
    assert _no_children()


def test_unexpected_exception_in_child_is_retryable_failure():
    with pytest.raises(IsolatedExtractionFailed) as info:
        run_isolated(int, "不是數字", timeout=30)
    assert "ValueError" in str(info.value)


def test_should_stop_interrupts_and_kills_child():
    started = time.monotonic()
    with pytest.raises(ExtractionInterrupted):
        run_isolated(time.sleep, 3600, timeout=600, should_stop=lambda: True)
    assert time.monotonic() - started < 30
    assert _no_children()


def test_timeout_must_be_positive():
    with pytest.raises(ValueError):
        run_isolated(time.sleep, 0, timeout=0)
