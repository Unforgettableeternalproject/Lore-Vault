"""pdf 抽取器（T-61，依 T-57 裁決）：`pypdf`。

每頁一個 segment（locator `page`，1 起算）。

- 開頭 1KB 內沒有 `%PDF-` → unsupported_format（副檔名與內容不符）
- 加密：先試空密碼（只設 owner password 的 pdf 可讀）；解不開 → encrypted
- 每頁抽出後先做 CJK 字間空白合併（`merge_cjk_spacing`，只作用於 pdf），
  再判斷是否空白頁；空白頁不產 segment
- 解析例外 → corrupt；empty_extraction 的字數門檻在 `extract()` 統一判定
"""

from __future__ import annotations

import io

from pypdf import PasswordType, PdfReader

from .base import (
    CORRUPT,
    ENCRYPTED,
    UNSUPPORTED_FORMAT,
    Budget,
    ExtractionError,
    Locator,
    Segment,
    merge_cjk_spacing,
)

_HEADER_WINDOW = 1024


def extract_pdf(data: bytes, budget: Budget) -> list[Segment]:
    if b"%PDF-" not in data[:_HEADER_WINDOW]:
        raise ExtractionError(UNSUPPORTED_FORMAT, "不是 pdf（找不到 %PDF- 檔頭）")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(CORRUPT, f"pdf 解析失敗：{exc}") from None
    if reader.is_encrypted:
        try:
            result = reader.decrypt("")
        except Exception as exc:
            raise ExtractionError(ENCRYPTED, f"pdf 已加密且無法解密：{exc}") from None
        if result == PasswordType.NOT_DECRYPTED:
            raise ExtractionError(ENCRYPTED, "pdf 已加密（需要密碼）")
    try:
        pages = list(reader.pages)
    except Exception as exc:
        raise ExtractionError(CORRUPT, f"pdf 頁面結構損毀：{exc}") from None
    segments: list[Segment] = []
    for number, page in enumerate(pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:
            raise ExtractionError(
                CORRUPT, f"pdf 第 {number} 頁抽取失敗：{exc}"
            ) from None
        text = merge_cjk_spacing(raw).strip()
        if text:
            segments.append(Segment(budget.add(text), Locator("page", number)))
    return segments
