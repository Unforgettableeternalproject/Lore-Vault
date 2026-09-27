"""文件抽取器（A19，T-60～T-62）：純函式，bytes + 檔名／MIME → 結構化段落清單。

    extract(data, filename, mime=None, *, limits=Limits()) -> Extraction

- 成功回傳 `Extraction(format, segments, char_count, garbled_chars)`，
  segments 必定非空；失敗一律拋 `ExtractionError(code, detail)`，
  錯誤碼見 `ERROR_CODES`。
  「抽出空結果」不會被當成功（empty_extraction）。
- 不碰儲存層與設定載入；上限由呼叫端以 `Limits`（`Limits.from_config`）傳入。
- segment 是結構單位（標題區段／頁／投影片／整份純文字），不是 chunk；
  定長切段、重疊與 `part` 屬 T-63 之後。

格式判定（`detect_format`）：
1. 副檔名優先（大小寫不拘）：`.md .markdown`→md、`.txt`→txt、`.json`、`.yaml .yml`、
   `.toml`、`.pdf`、`.docx`、`.pptx`。
2. 其他純文字檔一律當 txt：副檔名在 `TEXT_EXTENSIONS` 白名單
   （程式碼、設定、標記語言等），或檔名本身在 `TEXT_FILENAMES`
   （Dockerfile、Makefile 等無副檔名的慣例檔）。
3. 副檔名不認識（或沒有）時才看 MIME：已知的文件 MIME 對應格式，`text/*` → txt。
   客戶端常送 `application/octet-stream`，所以 MIME 不能蓋過副檔名。
4. 以上皆不符 → unsupported_format。

判定為文字類（md／txt／json／yaml／toml）後還要通過內容檢查（`decode_text`）：
依序試 UTF-8（可帶 BOM）→ 帶 BOM 的 UTF-16 → cp950（Big5，台灣常見）；cp950 必須
嚴格解碼成功且通過文字性檢查才採用。含 NUL 或控制字元比例 > 1% 視為二進位
（unsupported_format）；都不符 → unsupported_encoding。偵測到的編碼記在
`Extraction.encoding`。cp950 判定樣本小（非 ASCII 字元 < 50）或亂碼偏多時，
`Extraction.warnings` 帶 `encoding_low_confidence`（仍算成功）。
pdf／docx／pptx 另驗檔頭魔數，副檔名與內容不符 → unsupported_format。

空結果：任何格式抽不出可見字元都是 empty_extraction；另外只有 pdf 在去空白字數
< `min_chars` 時也算（多半是掃描件）。docx／pptx 與文字格式不套門檻（短簡報、
短設定檔是正常內容）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath

from .base import (
    CORRUPT,
    EMPTY_EXTRACTION,
    ENCODING_LOW_CONFIDENCE,
    ENCRYPTED,
    ERROR_CODES,
    LOCATOR_KINDS,
    TOO_LARGE,
    UNSUPPORTED_ENCODING,
    UNSUPPORTED_FORMAT,
    Budget,
    Extraction,
    ExtractionError,
    ExtractionWarning,
    Limits,
    Locator,
    Segment,
    clean_text,
    encoding_warnings,
    garbled_chars,
    merge_cjk_spacing,
    visible_chars,
)

__all__ = [
    "BINARY_FORMATS",
    "CORRUPT",
    "EMPTY_EXTRACTION",
    "ENCODING_LOW_CONFIDENCE",
    "ENCRYPTED",
    "ERROR_CODES",
    "FORMATS",
    "FORMAT_MIMES",
    "LOCATOR_KINDS",
    "MIN_CHARS_FORMATS",
    "TEXT_EXTENSIONS",
    "TEXT_FILENAMES",
    "TOO_LARGE",
    "UNSUPPORTED_ENCODING",
    "UNSUPPORTED_FORMAT",
    "Extraction",
    "ExtractionError",
    "ExtractionWarning",
    "Limits",
    "Locator",
    "Segment",
    "detect_format",
    "extract",
    "merge_cjk_spacing",
    "mime_from_filename",
]

FORMATS = ("md", "txt", "json", "yaml", "toml", "pdf", "docx", "pptx")
BINARY_FORMATS = frozenset({"pdf", "docx", "pptx"})
# 套 min_chars 門檻的格式（B1 裁決：只有 pdf，用來判掃描件）。docx／pptx 與文字格式
# 只在完全沒有可見字元時才 empty_extraction（短簡報、十幾個字的設定檔是正常內容）。
MIN_CHARS_FORMATS = frozenset({"pdf"})

_EXTENSIONS = {
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
}

# 當 txt 處理的其他純文字副檔名（仍須通過 UTF-8／非二進位的內容檢查）
TEXT_EXTENSIONS = frozenset(
    {
        # 文件與標記
        ".rst", ".adoc", ".asciidoc", ".org", ".tex", ".log", ".csv", ".tsv",
        ".html", ".htm", ".xml", ".svg", ".xhtml",
        # 設定
        ".ini", ".cfg", ".conf", ".properties", ".editorconfig",
        ".jsonc", ".json5", ".ndjson", ".jsonl", ".lock",
        # 程式碼
        ".py", ".pyi", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".vue", ".svelte", ".css", ".scss", ".sass", ".less",
        ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".fs", ".vb",
        ".go", ".rs", ".swift", ".m", ".mm", ".dart", ".rb", ".php", ".pl",
        ".lua", ".r", ".jl", ".ex", ".exs", ".erl", ".hs", ".clj", ".elm",
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
        ".sql", ".graphql", ".gql", ".proto", ".tf", ".hcl", ".nix",
        ".cmake", ".mk", ".dockerfile", ".gitignore", ".gitattributes",
    }
)  # fmt: skip

TEXT_FILENAMES = frozenset(
    {
        "dockerfile",
        "makefile",
        "gnumakefile",
        "cmakelists.txt",
        "license",
        "readme",
        "changelog",
        "authors",
        "notice",
        "procfile",
        "gemfile",
        "rakefile",
        "vagrantfile",
        "jenkinsfile",
    }
)

_MIMES = {
    "text/markdown": "md",
    "text/x-markdown": "md",
    "application/json": "json",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "text/x-yaml": "yaml",
    "application/toml": "toml",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "pptx"
    ),
}


# 各格式的標準 MIME（上傳時客戶端沒給具體 MIME 就依副檔名推定用；
# 刻意不用 `mimetypes`，它會讀系統登錄／mime.types，不同機器結果不同）
FORMAT_MIMES = {
    "md": "text/markdown",
    "txt": "text/plain",
    "json": "application/json",
    "yaml": "application/yaml",
    "toml": "application/toml",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}


def mime_from_filename(filename: str) -> str | None:
    """只依檔名（副檔名／慣例檔名）推定標準 MIME；判定規則同 `detect_format`。

    認不出來回 None（不看客戶端 MIME）。
    """
    try:
        return FORMAT_MIMES[detect_format(filename)]
    except ExtractionError:
        return None


def detect_format(filename: str, mime: str | None = None) -> str:
    """依檔名（與退回用的 MIME）決定格式。

    不支援 → `ExtractionError(unsupported_format)`。
    """
    name = PurePath(filename.replace("\\", "/")).name.lower()
    suffix = PurePath(name).suffix
    if suffix in _EXTENSIONS:
        return _EXTENSIONS[suffix]
    if suffix in TEXT_EXTENSIONS or name in TEXT_FILENAMES:
        return "txt"
    if mime:
        base = mime.split(";", 1)[0].strip().lower()
        if base in _MIMES:
            return _MIMES[base]
        if base.startswith("text/"):
            return "txt"
    raise ExtractionError(
        UNSUPPORTED_FORMAT,
        f"不支援的檔案類型：{filename!r}（MIME {mime or '未提供'}）",
    )


def _extractor(fmt: str) -> Callable[[bytes, Budget, Limits], list[Segment]]:
    # 延遲 import：用不到 pdf／office 套件的呼叫端不付載入成本
    if fmt in ("md", "txt"):
        from . import text

        func = text.extract_md if fmt == "md" else text.extract_txt
        return lambda data, budget, _limits: func(data, budget)
    if fmt in ("json", "yaml", "toml"):
        from . import structured

        func = getattr(structured, f"extract_{fmt}")
        return lambda data, budget, _limits: func(data, budget)
    if fmt == "pdf":
        from .pdf import extract_pdf

        return lambda data, budget, _limits: extract_pdf(data, budget)
    if fmt == "docx":
        from .docx import extract_docx

        return extract_docx
    if fmt == "pptx":
        from .pptx import extract_pptx

        return extract_pptx
    raise ExtractionError(UNSUPPORTED_FORMAT, f"沒有 {fmt} 的抽取器")


def extract(
    data: bytes,
    filename: str,
    mime: str | None = None,
    *,
    limits: Limits | None = None,
) -> Extraction:
    """抽取文件文字。成功回 `Extraction`（segments 非空），失敗拋 `ExtractionError`。"""
    limits = limits if limits is not None else Limits()
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError(f"data 必須是 bytes，得到 {type(data).__name__}")
    data = bytes(data)
    if len(data) > limits.max_bytes:
        raise ExtractionError(
            TOO_LARGE, f"檔案 {len(data)} 位元組，超過上限 {limits.max_bytes}"
        )
    fmt = detect_format(filename, mime)
    budget = Budget(limits.max_chars)
    # 載入抽取器（含第三方套件 import）放在 try 外：缺套件是部署錯誤，
    # 不可被下面的 catch-all 誤標成每份文件都 corrupt
    extractor = _extractor(fmt)
    try:
        raw = extractor(data, budget, limits)
    except ExtractionError:
        raise
    except MemoryError:
        raise ExtractionError(TOO_LARGE, "抽取時記憶體不足") from None
    except Exception as exc:  # 解析套件的未預期例外一律視為檔案損毀，不外洩成 500
        raise ExtractionError(CORRUPT, f"{type(exc).__name__}: {exc}") from None
    segments = []
    for segment in raw:
        text = clean_text(segment.text)
        if text.strip():
            segments.append(Segment(text, segment.locator))
    chars = sum(visible_chars(s.text) for s in segments)
    if chars == 0:
        raise ExtractionError(EMPTY_EXTRACTION, "抽不出任何文字")
    if fmt in MIN_CHARS_FORMATS and chars < limits.min_chars:
        raise ExtractionError(
            EMPTY_EXTRACTION,
            f"只抽出 {chars} 個字（門檻 {limits.min_chars}），多半是掃描件或純圖片",
        )
    garbled = sum(garbled_chars(s.text) for s in segments)
    return Extraction(
        format=fmt,
        segments=tuple(segments),
        char_count=chars,
        garbled_chars=garbled,
        encoding=budget.encoding,
        warnings=encoding_warnings(
            budget.encoding,
            non_ascii=sum(1 for s in segments for ch in s.text if ord(ch) > 0x7F),
            garbled=garbled,
            visible=chars,
        ),
    )
