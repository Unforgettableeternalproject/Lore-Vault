"""T-60：格式判定、純文字／md／json／yaml／toml 抽取與共用錯誤路徑。"""

from __future__ import annotations

import pytest

from lore_vault.documents.extract import (
    CORRUPT,
    EMPTY_EXTRACTION,
    TOO_LARGE,
    UNSUPPORTED_ENCODING,
    UNSUPPORTED_FORMAT,
    ExtractionError,
    Limits,
    detect_format,
    extract,
)


def _err(data: bytes, name: str, mime: str | None = None, **limits) -> ExtractionError:
    with pytest.raises(ExtractionError) as info:
        extract(data, name, mime, limits=Limits(**limits) if limits else None)
    return info.value


def _segments(result):
    return [(s.locator.to_dict(), s.text) for s in result.segments]


# ── 格式判定 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("README.MD", None, "md"),
        ("notes.markdown", None, "md"),
        ("a.txt", None, "txt"),
        ("cfg.YML", None, "yaml"),
        ("pyproject.toml", None, "toml"),
        ("data.json", None, "json"),
        ("x.pdf", "application/octet-stream", "pdf"),
        ("x.docx", None, "docx"),
        ("deck.pptx", None, "pptx"),
        # 其他純文字檔一律 txt
        ("main.py", None, "txt"),
        ("lib.rs", None, "txt"),
        ("Dockerfile", None, "txt"),
        ("C:\\repo\\Makefile", None, "txt"),
        # 副檔名不認識才退回 MIME
        ("noext", "text/plain; charset=utf-8", "txt"),
        ("blob", "application/pdf", "pdf"),
        # MIME 不能蓋過副檔名
        ("a.md", "application/pdf", "md"),
    ],
)
def test_detect_format(name, mime, expected):
    assert detect_format(name, mime) == expected


@pytest.mark.parametrize(
    ("name", "mime"),
    [("photo.png", None), ("a.exe", "application/octet-stream"), ("x", None)],
)
def test_detect_format_rejects_unknown(name, mime):
    with pytest.raises(ExtractionError) as info:
        detect_format(name, mime)
    assert info.value.code == UNSUPPORTED_FORMAT


# ── txt ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "記憶系統的設計筆記。\n第二行內容。",
        "混合 English 與中文的段落\r\n以 CRLF 換行",
        "\ufeff帶 BOM 的 UTF-8 文字檔",
    ],
)
def test_txt_is_one_offset_segment(text):
    result = extract(text.encode("utf-8"), "note.txt")
    assert result.format == "txt"
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.locator.to_dict() == {"kind": "offset", "value": 0}
    assert "\r" not in segment.text and "\ufeff" not in segment.text
    assert result.char_count > 0


def test_source_code_is_treated_as_txt():
    code = 'def 問候(name):\n    return f"你好 {name}"\n'
    result = extract(code.encode(), "hello.py")
    assert result.format == "txt"
    assert result.segments[0].text.startswith("def 問候")


def test_utf16_with_bom_is_accepted():
    result = extract("UTF-16 的中文檔案".encode("utf-16"), "a.txt")
    assert result.segments[0].text == "UTF-16 的中文檔案"


def test_non_utf8_text_is_unsupported_encoding():
    assert (
        _err("繁體中文 Big5 檔案".encode("big5"), "a.txt").code == UNSUPPORTED_ENCODING
    )


@pytest.mark.parametrize(
    "data",
    [b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", bytes(range(1, 32)) * 10],
)
def test_binary_content_with_text_extension_is_rejected(data):
    assert _err(data, "fake.py").code == UNSUPPORTED_FORMAT


def test_blank_text_is_empty_extraction_not_success():
    assert _err(b"  \n\t\n", "a.txt").code == EMPTY_EXTRACTION
    assert _err(b"", "a.md").code == EMPTY_EXTRACTION


def test_short_text_file_is_fine():
    """文字格式不套 min_chars：短設定檔是正常內容。"""
    assert extract(b"ok", "a.txt").char_count == 2


def test_file_size_limit():
    assert _err(b"x" * 11, "a.txt", max_bytes=10).code == TOO_LARGE


def test_char_limit():
    assert _err("字".encode() * 20, "a.txt", max_chars=10).code == TOO_LARGE


def test_forbidden_control_characters_are_cleaned():
    # 控制字元比例要低於二進位判定門檻（1%）
    text = "前\x07後" + "正常內容" * 50
    result = extract(text.encode(), "a.txt")
    assert "\x07" not in result.segments[0].text


# ── md ──────────────────────────────────────────────────────────────


def test_md_nested_heading_paths():
    md = """前言，在任何標題之前。

# 設定

總覽內容。

## 資料庫

使用 SQLite。

### 備份

每日備份。

## 快取

記憶體快取。

# 部署 #

docker compose。
"""
    result = extract(md.encode(), "設計.md")
    assert [s.locator.to_dict() for s in result.segments] == [
        {"kind": "offset", "value": 0},
        {"kind": "heading", "value": "設定"},
        {"kind": "heading", "value": "設定 > 資料庫"},
        {"kind": "heading", "value": "設定 > 資料庫 > 備份"},
        {"kind": "heading", "value": "設定 > 快取"},
        {"kind": "heading", "value": "部署"},
    ]
    backup = result.segments[3].text
    assert backup.startswith("備份") and "每日備份。" in backup


def test_md_heading_inside_code_fence_is_not_a_heading():
    md = """# 範例

```bash
# 這是註解，不是標題
echo hi
```

~~~
## 也不是
~~~

## 真的小節

內容。
"""
    result = extract(md.encode(), "a.md")
    locators = [s.locator.value for s in result.segments]
    assert locators == ["範例", "範例 > 真的小節"]
    assert "# 這是註解" in result.segments[0].text


def test_md_level_jump_and_hashtag_text():
    md = "### 深層標題\n內容\n#不是標題（缺空白）\n# 頂層\n正文"
    result = extract(md.encode(), "a.md")
    assert _segments(result) == [
        (
            {"kind": "heading", "value": "深層標題"},
            "深層標題\n內容\n#不是標題（缺空白）",
        ),
        ({"kind": "heading", "value": "頂層"}, "頂層\n正文"),
    ]


# ── json／yaml／toml ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "data", "expected_lines"),
    [
        (
            "a.json",
            '{"專案": "記憶庫", "版本": 8, "啟用": true, "標籤": ["dev", "lore"], '
            '"db": {"路徑": null, "選項": {}}}',
            [
                "專案: 記憶庫",
                "版本: 8",
                "啟用: true",
                "標籤[0]: dev",
                "標籤[1]: lore",
                "db.路徑: null",
                "db.選項: {}",
            ],
        ),
        ("b.json", '["甲", {"乙": []}]', ["[0]: 甲", "[1].乙: []"]),
        ("c.json", '"只有一個字串的 JSON"', ["只有一個字串的 JSON"]),
        (
            "a.yaml",
            "名稱: 世界觀\n角色:\n  - 名字: 諾薇亞\n    職責: 實作\n日期: 2026-09-26\n",
            [
                "名稱: 世界觀",
                "角色[0].名字: 諾薇亞",
                "角色[0].職責: 實作",
                "日期: 2026-09-26",
            ],
        ),
        (
            "b.yml",
            "---\n第一份: 甲\n---\n第二份: 乙\n",
            ["第一份: 甲", "", "第二份: 乙"],
        ),
        ("c.yaml", "- 單純清單\n- 第二項\n", ["[0]: 單純清單", "[1]: 第二項"]),
        (
            "a.toml",
            '["伺服器"]\n"主機" = "localhost"\n"埠" = 5056\n\n'
            '[["使用者"]]\n"名" = "甲"\n',
            ["伺服器.主機: localhost", "伺服器.埠: 5056", "使用者[0].名: 甲"],
        ),
        ("b.toml", 'title = "中文標題"\n', ["title: 中文標題"]),
        (
            "c.toml",
            "when = 2026-09-26T00:00:00Z\nflags = [true, false]\n",
            ["when: 2026-09-26T00:00:00+00:00", "flags[0]: true", "flags[1]: false"],
        ),
    ],
)
def test_structured_formats_become_key_path_lines(name, data, expected_lines):
    result = extract(data.encode(), name)
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.locator.to_dict() == {"kind": "offset", "value": 0}
    assert segment.text.split("\n") == expected_lines


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.json", "{not json"),
        pytest.param("a.json", "[" * 100_000, id="deep-json"),
        ("a.yaml", "a: [unclosed"),
        ("a.toml", "a = "),
        ("a.yaml", "a: !!python/object:os.system {}"),  # safe loader 不建構任意物件
    ],
)
def test_invalid_structured_data_is_corrupt(name, data):
    assert _err(data.encode(), name).code == CORRUPT


def test_yaml_alias_bomb_is_too_large_not_hang():
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    prev = "a"
    for level in "bcdefghij":
        bomb += f"{level}: &{level} [" + ", ".join([f"*{prev}"] * 10) + "]\n"
        prev = level
    assert _err(bomb.encode(), "bomb.yaml", max_chars=100_000).code == TOO_LARGE


def test_yaml_self_reference_is_corrupt():
    assert _err(b"a: &a [1, *a]\n", "loop.yaml").code == CORRUPT


def test_empty_structured_document_is_empty_extraction():
    assert _err(b"# only a comment\n", "a.yaml").code == EMPTY_EXTRACTION


def test_missing_parser_dependency_is_not_reported_as_corrupt(monkeypatch):
    """缺套件是部署錯誤：不可被 catch-all 吞成每份文件都 corrupt。"""
    import lore_vault.documents.extract as extract_mod

    def broken(fmt):
        raise ImportError("模擬缺少 pypdf")

    monkeypatch.setattr(extract_mod, "_extractor", broken)
    with pytest.raises(ImportError):
        extract(b"%PDF-1.4", "a.pdf")


def test_limits_from_config():
    from lore_vault.config import load_config

    config = load_config(
        environ={
            "LORE_VAULT_DOCUMENTS_MAX_CHARS": "123",
            "LORE_VAULT_DOCUMENTS_MIN_CHARS": "7",
            "LORE_VAULT_DOCUMENTS_BLOB_DIR": "/data/blobs",
        }
    )
    limits = Limits.from_config(config.documents)
    assert (limits.max_bytes, limits.max_chars, limits.min_chars) == (
        25 * 1024 * 1024,
        123,
        7,
    )
    assert config.documents.blob_dir == "/data/blobs"


@pytest.mark.parametrize(
    ("key", "value"),
    [("MAX_FILE_BYTES", "0"), ("MAX_CHARS", "-1"), ("MIN_CHARS", "-1")],
)
def test_documents_config_is_validated(key, value):
    from lore_vault.config import ConfigError, load_config

    with pytest.raises(ConfigError):
        load_config(environ={f"LORE_VAULT_DOCUMENTS_{key}": value})
