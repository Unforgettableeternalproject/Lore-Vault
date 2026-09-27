"""抽取器測試的自造樣本（全部在測試中程式產生，不讀任何真實文件）。"""

from __future__ import annotations

import io

import pytest


def _pdf(pages: list[str], encrypt: dict | None = None) -> bytes:
    """以 Helvetica 寫 ASCII 內容流的 pdf（空字串 = 空白頁）。

    CJK 需要 CID 字型、沒有 reportlab 做不出來；CJK 合併規則另以純函式測。
    """
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(612, 792)
        if not text:
            continue
        stream = StreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    if encrypt:
        writer.encrypt(**encrypt)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _docx(build) -> bytes:
    import docx

    document = docx.Document()
    build(document)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _pptx(build) -> bytes:
    import pptx

    presentation = pptx.Presentation()
    build(presentation)
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


@pytest.fixture
def make_pdf():
    return _pdf


@pytest.fixture
def make_docx():
    return _docx


@pytest.fixture
def make_pptx():
    return _pptx
