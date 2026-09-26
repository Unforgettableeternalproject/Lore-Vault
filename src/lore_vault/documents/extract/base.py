"""抽取器共用型別、錯誤碼、上限與文字處理（純標準庫）。"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

from lore_vault.schema.chars import FORBIDDEN_CONTROL

# ── 錯誤碼（與 storage.migrate.DOCUMENT_ERROR_CODES 同步，測試比對）──

ENCRYPTED = "encrypted"
CORRUPT = "corrupt"
EMPTY_EXTRACTION = "empty_extraction"
TOO_LARGE = "too_large"
UNSUPPORTED_FORMAT = "unsupported_format"
UNSUPPORTED_ENCODING = "unsupported_encoding"
ERROR_CODES = (
    ENCRYPTED,
    CORRUPT,
    EMPTY_EXTRACTION,
    TOO_LARGE,
    UNSUPPORTED_FORMAT,
    UNSUPPORTED_ENCODING,
)


class ExtractionError(Exception):
    """抽取失敗。`code` 必在 `ERROR_CODES` 內，`detail` 是給人看的原因。"""

    def __init__(self, code: str, detail: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"未知的抽取錯誤碼：{code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ── 位置與結果 ─────────────────────────────────────────────────────

# heading：標題路徑（"A > B"）；page／slide：1 起算；offset：抽出文字內的字元位置；
# header／footer：docx 頁首／頁尾，value 為節（section）序號（1 起算）
LOCATOR_KINDS = frozenset({"heading", "page", "slide", "offset", "header", "footer"})
HEADING_SEPARATOR = " > "


@dataclass(frozen=True)
class Locator:
    kind: str
    value: int | str

    def __post_init__(self) -> None:
        if self.kind not in LOCATOR_KINDS:
            raise ValueError(f"未知的 locator kind：{self.kind!r}")
        if self.kind == "heading":
            if not isinstance(self.value, str):
                raise TypeError("heading locator 的 value 必須是字串")
        elif isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError(f"{self.kind} locator 的 value 必須是整數")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class Segment:
    """一個結構單位（md／docx 的標題區段、pdf 的頁、pptx 的投影片、整份純文字）。

    不是 chunk：定長切段與重疊屬 T-63 之後，會在 segment 內再切並加 `part`。
    """

    text: str
    locator: Locator

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("segment 不可為空白")


# 品質警示碼（不影響成敗，記在 documents.warnings、doctor 以 warn 列出）
ENCODING_LOW_CONFIDENCE = "encoding_low_confidence"


@dataclass(frozen=True)
class ExtractionWarning:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Extraction:
    """抽取成功的結果。segments 必定非空——「空結果」只會以 `ExtractionError` 表示。"""

    format: str
    segments: tuple[Segment, ...]
    # 去空白後的字元數（empty_extraction 門檻比的就是它）
    char_count: int
    # 亂碼跡象：U+FFFD、私用區字元、`(cid:n)` 殘留的總數（品質警示，不影響成敗）
    garbled_chars: int = 0
    # 文字類格式偵測到的編碼（utf-8／utf-8-sig／utf-16／cp950）；二進位格式為 None
    encoding: str | None = None
    # 品質警示（例如 cp950 判定樣本太小）；成功抽取但結果可能不可靠
    warnings: tuple[ExtractionWarning, ...] = ()

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("Extraction 必須至少有一個 segment")
        if self.char_count <= 0:
            raise ValueError("Extraction 的 char_count 必須大於 0")


@dataclass(frozen=True)
class Limits:
    """上限（A19）：單檔 25MB、抽出文字 200 萬字元；pdf 去空白後少於 `min_chars`
    視為 empty_extraction（T-57 裁決；B1 裁決限縮為只套 pdf）。"""

    max_bytes: int = 25 * 1024 * 1024
    max_chars: int = 2_000_000
    min_chars: int = 50
    # docx／pptx（zip）解壓後總大小上限 = max_bytes 的倍數，擋壓縮炸彈
    unzip_ratio: int = 8
    # zip 成員數上限（正常 Office 檔數十到數百個成員）
    max_zip_members: int = 10_000
    # 單一成員實際解壓量 / 壓縮量上限；解壓量未達 `ZIP_RATIO_FLOOR` 不檢查。
    # 正常 Office XML 多在 5–50 倍，大量重複的表格也少見超過 100 倍；
    # DEFLATE 理論上限約 1032 倍
    max_member_ratio: int = 200

    def __post_init__(self) -> None:
        if (
            self.max_bytes <= 0
            or self.max_chars <= 0
            or self.unzip_ratio <= 0
            or self.max_zip_members <= 0
            or self.max_member_ratio <= 0
        ):
            raise ValueError(
                "max_bytes／max_chars／unzip_ratio／max_zip_members／"
                "max_member_ratio 必須大於 0"
            )
        if self.min_chars < 0:
            raise ValueError("min_chars 不可為負")

    @property
    def max_unzipped_bytes(self) -> int:
        return self.max_bytes * self.unzip_ratio

    @classmethod
    def from_config(cls, documents: Any) -> Limits:
        """由 `config.DocumentsConfig` 建立。

        不 import config：抽取器不依賴設定載入。
        """
        return cls(
            max_bytes=documents.max_file_bytes,
            max_chars=documents.max_chars,
            min_chars=documents.min_chars,
        )


class Budget:
    """邊累加邊檢查抽出文字總長；超過即 too_large。

    YAML 別名展開之類的膨脹不會先耗盡記憶體才被發現。
    """

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.used = 0
        # 文字類抽取器解碼時記下偵測到的編碼（`decode_text`），交給 Extraction
        self.encoding: str | None = None

    def add(self, text: str) -> str:
        self.used += len(text)
        if self.used > self.max_chars:
            raise ExtractionError(TOO_LARGE, f"抽出文字超過上限 {self.max_chars} 字元")
        return text


# ── 文字處理 ───────────────────────────────────────────────────────

# 與 storage/fts.py 相同的表意文字範圍，另加 CJK 標點（、。「」等）與全形符號，
# 用於 pdf 的字間空白合併
_CJK_RANGES = (
    ("぀", "ヿ"),
    ("㐀", "䶿"),
    ("一", "鿿"),
    ("가", "힯"),
    ("豈", "﫿"),
    ("\U00020000", "\U0002fa1f"),
    ("、", "〿"),
    ("！", "｠"),
)
CJK_CLASS = "".join(f"{lo}-{hi}" for lo, hi in _CJK_RANGES)
_CJK_SPACE_RE = re.compile(f"(?<=[{CJK_CLASS}])[ \\t]{{1,3}}(?=[{CJK_CLASS}])")
_CJK_NEWLINE_RE = re.compile(f"(?<=[{CJK_CLASS}])\\n(?=[{CJK_CLASS}])")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def merge_cjk_spacing(text: str) -> str:
    """pdf 抽取後處理（T-57 裁決）：移除 CJK 字之間被插入的 1–3 個空白／tab，
    以及 CJK 字之間的單一換行。段落分隔（連續兩個以上換行）與中英交界的空格不動。"""
    text = normalize_newlines(text)
    text = _CJK_SPACE_RE.sub("", text)
    return _CJK_NEWLINE_RE.sub("", text)


def visible_chars(text: str) -> int:
    """去空白後的字元數。"""
    return sum(1 for ch in text if not ch.isspace())


_CID_RE = re.compile(r"\(cid:\d+\)")


def garbled_chars(text: str) -> int:
    count = sum(
        1
        for ch in text
        if ch == "\ufffd"
        or "\ue000" <= ch <= "\uf8ff"
        or "\U000f0000" <= ch <= "\U0010ffff"
    )
    return count + sum(len(m.group(0)) for m in _CID_RE.finditer(text))


def clean_text(text: str) -> str:
    """換行正規化；禁用控制字元（NUL 等）換成空白、孤立 surrogate 換成 U+FFFD，
    讓下游寫入不會被 `schema.chars` 擋下。"""
    text = normalize_newlines(text)
    out: list[str] = []
    for ch in text:
        if ch in FORBIDDEN_CONTROL:
            out.append(" ")
        elif "\ud800" <= ch <= "\udfff":
            out.append("\ufffd")
        else:
            out.append(ch)
    return "".join(out)


_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF8_BOM = b"\xef\xbb\xbf"
# 允許的 C0 控制字元：tab、換行、歸位、換頁、垂直 tab、ESC（終端色碼記錄檔）
_TEXT_CONTROL_OK = frozenset("\t\n\r\f\v\x1b")
# 其他 C0 控制字元佔比超過此值 → 視為二進位
_BINARY_CONTROL_RATIO = 0.01
# cp950 文字性檢查：非 ASCII 字元中「中文字、注音、CJK 標點、全形符號」至少佔此比例。
# cp950 對 Latin-1／cp1252 等其他 8-bit 編碼的高位元組配對常能解出合法漢字，
# 光靠嚴格解碼成功不足以判定是 Big5
_CP950_CJK_RATIO = 0.9
# 中文字中 Big5 常用字（第一級）至少佔此比例
_CP950_COMMON_RATIO = 0.8
CP950 = "cp950"
# cp950 判定的信心門檻：非 ASCII 字元少於此數（樣本太小，文字性檢查的比例不可靠）、
# 或亂碼跡象（`garbled_chars`）佔可見字元超過此比例 → encoding_low_confidence
CP950_MIN_NON_ASCII = 50
CP950_MAX_GARBLED_RATIO = 0.01
_CP950_TEXT_RE = re.compile(f"[{CJK_CLASS}㄀-ㄯㆠ-ㆿ]")


def _check_controls(text: str) -> None:
    if text:
        controls = sum(1 for ch in text if ch < " " and ch not in _TEXT_CONTROL_OK)
        if controls / len(text) > _BINARY_CONTROL_RATIO:
            raise ExtractionError(
                UNSUPPORTED_FORMAT, "控制字元比例過高，判定為二進位檔"
            )


def _is_big5_common(ch: str) -> bool:
    """Big5 常用字（第一級，lead byte 0xA4–0xC6）。"""
    try:
        return 0xA4 <= ch.encode(CP950)[0] <= 0xC6
    except UnicodeEncodeError:
        return False


def looks_like_cp950_text(text: str) -> bool:
    """cp950 解出的文字是否像真的 Big5 文字：

    - 沒有使用者自定區（私用區）字元
    - 非 ASCII 字元大多（≥ 90%）是中文字／注音／CJK 標點／全形符號
    - 中文字大多（≥ 80%）落在 Big5 常用字區：GBK 等其他雙位元組編碼誤解成 cp950
      時也是一串合法漢字，但會散進次常用字區（自造樣本實測：繁中文字 100%、
      GBK 誤解 ≤ 40%）
    """
    non_ascii = [ch for ch in text if ord(ch) > 0x7F]
    if not non_ascii:
        return False
    if any("" <= ch <= "" for ch in non_ascii):
        return False
    cjk = sum(1 for ch in non_ascii if _CP950_TEXT_RE.match(ch))
    if cjk / len(non_ascii) < _CP950_CJK_RATIO:
        return False
    hanzi = [ch for ch in non_ascii if "一" <= ch <= "鿿"]
    if not hanzi:
        return False
    common = sum(1 for ch in hanzi if _is_big5_common(ch))
    return common / len(hanzi) >= _CP950_COMMON_RATIO


def encoding_warnings(
    encoding: str | None, *, non_ascii: int, garbled: int, visible: int
) -> tuple[ExtractionWarning, ...]:
    """cp950 回退判定的信心檢查：通過文字性檢查但樣本小或亂碼偏多時給警示。"""
    if encoding != CP950:
        return ()
    reasons = []
    if non_ascii < CP950_MIN_NON_ASCII:
        reasons.append(
            f"非 ASCII 字元只有 {non_ascii} 個（門檻 {CP950_MIN_NON_ASCII}）"
        )
    if visible and garbled / visible > CP950_MAX_GARBLED_RATIO:
        reasons.append(f"亂碼跡象 {garbled} 個（可見字元 {visible}）")
    if not reasons:
        return ()
    return (
        ExtractionWarning(
            ENCODING_LOW_CONFIDENCE,
            "cp950（Big5）判定信心低：" + "；".join(reasons) + "，內容可能是其他編碼",
        ),
    )


def _decode(data: bytes) -> tuple[str, str]:
    """回傳 (文字, 編碼名)。依序：UTF-16 BOM → UTF-8（可帶 BOM）→ cp950。"""
    if data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as exc:
            raise ExtractionError(
                UNSUPPORTED_ENCODING, f"UTF-16 解碼失敗：{exc.reason}"
            ) from None
    if b"\x00" in data:
        raise ExtractionError(UNSUPPORTED_FORMAT, "內容含 NUL 位元組，判定為二進位檔")
    has_bom = data.startswith(_UTF8_BOM)
    try:
        if has_bom:
            return data[len(_UTF8_BOM) :].decode("utf-8"), "utf-8-sig"
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        position = exc.start
    if not has_bom:
        try:
            text = data.decode(CP950)
        except UnicodeDecodeError:
            pass
        else:
            _check_controls(text)
            if looks_like_cp950_text(text):
                return text, CP950
    raise ExtractionError(
        UNSUPPORTED_ENCODING,
        f"不是 UTF-8 或 Big5（cp950）文字（UTF-8 解碼失敗於位置 {position}）；"
        "請轉成 UTF-8 後再上傳",
    )


def decode_text(data: bytes, budget: Budget | None = None) -> str:
    """純文字判定與解碼：UTF-8（可帶 BOM）、帶 BOM 的 UTF-16，或 cp950（Big5）。

    - 含 NUL（UTF-16 以外）或其他 C0 控制字元佔比超過 1% → unsupported_format（二進位）
    - cp950 必須嚴格解碼成功且通過文字性檢查（`looks_like_cp950_text`）才採用
    - 都不符 → unsupported_encoding（例如 GBK、Shift_JIS、Latin-1 存檔的文字檔）

    有傳 `budget` 時把偵測到的編碼記在 `budget.encoding`。
    """
    text, encoding = _decode(data)
    _check_controls(text)
    if budget is not None:
        budget.encoding = encoding
    return normalize_newlines(text)


# ── Office（OOXML）容器檢查 ─────────────────────────────────────────

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
# 加密的 OOXML 會包進 OLE 容器，目錄項含 "EncryptedPackage"（UTF-16LE）
_OLE_ENCRYPTED_MARK = "EncryptedPackage".encode("utf-16-le")


# 串流解壓的分塊大小：記憶體峰值以此為量級，與宣告大小無關
ZIP_READ_CHUNK = 64 * 1024
# 單一成員解壓量低於此值時不檢查壓縮比（小檔的比例沒有意義）
ZIP_RATIO_FLOOR = 1024 * 1024


def _scan_zip_members(archive: zipfile.ZipFile, limits: Limits, label: str) -> None:
    """逐一串流解壓每個成員、累計**實際**位元組（不留內容），交給解析套件前確認：

    - 宣告的解壓後總大小不超過上限（竄改前的快速擋）
    - 實際解壓量累計超過上限立即中止 → too_large
    - 單一成員實際解壓量 / 壓縮量超過 `max_member_ratio` → too_large
    - 實際解壓量與宣告值（`ZipInfo.file_size`）不符、CRC 錯誤 → corrupt

    `ZipExtFile` 以 central directory 的 file_size 截斷輸出，所以宣告值被低報時
    讀到宣告量就會因 CRC 不符失敗；以固定大小分塊讀取，解壓器每次最多吐出一塊，
    不會像 `ZipFile.read()` 那樣一次把整個壓縮串流解進記憶體。
    """
    infos = archive.infolist()
    if len(infos) > limits.max_zip_members:
        raise ExtractionError(
            TOO_LARGE,
            f"{label} 的 zip 成員數 {len(infos)} 超過上限 {limits.max_zip_members}",
        )
    declared = sum(info.file_size for info in infos)
    if declared > limits.max_unzipped_bytes:
        raise ExtractionError(
            TOO_LARGE,
            f"{label} 解壓後 {declared} 位元組，超過上限 {limits.max_unzipped_bytes}",
        )
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        actual = 0
        with archive.open(info) as member:
            while chunk := member.read(ZIP_READ_CHUNK):
                actual += len(chunk)
                total += len(chunk)
                if total > limits.max_unzipped_bytes:
                    raise ExtractionError(
                        TOO_LARGE,
                        f"{label} 實際解壓超過上限 {limits.max_unzipped_bytes} 位元組",
                    )
                if actual > ZIP_RATIO_FLOOR and actual > limits.max_member_ratio * max(
                    info.compress_size, 1
                ):
                    raise ExtractionError(
                        TOO_LARGE,
                        f"{label} 的成員 {info.filename!r} 壓縮比超過 "
                        f"{limits.max_member_ratio} 倍（疑似壓縮炸彈）",
                    )
        if actual != info.file_size:
            raise ExtractionError(
                CORRUPT,
                f"{label} 的成員 {info.filename!r} 實際解壓 {actual} 位元組，"
                f"與宣告的 {info.file_size} 不符",
            )


def check_ooxml_container(data: bytes, limits: Limits, *, label: str) -> None:
    """docx／pptx 開檔前的容器判定：

    - OLE 容器且含 EncryptedPackage → encrypted（Office 以密碼加密）
    - 其他 OLE 容器 → unsupported_format（舊版 .doc／.ppt 改了副檔名）
    - 不是 zip → unsupported_format；zip 損毀（含 CRC、宣告大小不符）→ corrupt
    - 成員數、解壓後總大小（宣告與實際）、單一成員壓縮比超過上限 → too_large
      （壓縮炸彈；見 `_scan_zip_members`）
    """
    if data.startswith(_OLE_MAGIC):
        if _OLE_ENCRYPTED_MARK in data:
            raise ExtractionError(ENCRYPTED, f"{label} 已加密（需要密碼）")
        raise ExtractionError(
            UNSUPPORTED_FORMAT, f"不是 {label}（OLE 容器，可能是舊版二進位格式）"
        )
    if not data.startswith(_ZIP_MAGIC):
        raise ExtractionError(UNSUPPORTED_FORMAT, f"不是 {label}（不是 zip 容器）")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _scan_zip_members(archive, limits, label)
    except ExtractionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError) as exc:
        raise ExtractionError(CORRUPT, f"{label} 的 zip 容器損毀：{exc}") from None
    except (NotImplementedError, RuntimeError) as exc:
        # 不支援的壓縮法、加密成員
        raise ExtractionError(CORRUPT, f"{label} 的 zip 成員無法解壓：{exc}") from None
    except zlib.error as exc:
        raise ExtractionError(CORRUPT, f"{label} 的 zip 成員解壓失敗：{exc}") from None
