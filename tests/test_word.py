import zipfile
from xml.etree import ElementTree

import pymupdf
import pytest
from PySide6.QtGui import QAction

import epub
import word
from pdf_reader import Viewer
from test_epub import book  # noqa: F401  (the 4-page novel fixture)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def paragraphs(path):
    """[(style or None, page break before?, text, has picture)] from word/document.xml."""
    root = ElementTree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    out = []
    for p in root.iter(f"{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        out.append((style.get(f"{W}val") if style is not None else None,
                    p.find(f"{W}pPr/{W}pageBreakBefore") is not None,
                    "".join(t.text or "" for t in p.iter(f"{W}t")),
                    p.find(f".//{W}drawing") is not None))
    return out


def test_word_structure(book, tmp_path):
    out = tmp_path / "novel.docx"
    word.convert(str(book), str(out), "flow")
    z = zipfile.ZipFile(out)
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml",
            "word/media/image1.jpg"} <= set(z.namelist())
    assert "<dc:title>The Novel</dc:title>" in z.read("docProps/core.xml").decode()
    assert 'w:lang w:val="en-US"' in z.read("word/styles.xml").decode(), "spell-check language"
    paras = paragraphs(out)
    headings = [(text, brk) for style, brk, text, _ in paras if style == "Heading1"]
    assert headings == [("Chapter One", False), ("Chapter Two", True)], "chapter 2 starts a new page"
    text = " ".join(p[2] for p in paras)
    assert "THE NOVEL" not in text, "running head dropped"
    assert not any(p[2].isdigit() for p in paras), "page numbers dropped"
    assert "Winston made for the stairs, slowly" in text, "sentence split by the page turn is rejoined"
    assert sum(p[3] for p in paras) == 1, "figure kept"


@pytest.fixture
def poster(tmp_path):
    """Page 1: body text over a coloured shape, big lettering, sideways text, hidden OCR text. Page 2: text only."""
    path = tmp_path / "poster.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.draw_rect(pymupdf.Rect(0, 0, 400, 250), color=None, fill=(1, 0.9, 0.3))
    page.insert_text((40, 80), "BIG TITLE", fontsize=40)
    page.insert_text((380, 400), "Sideways", fontsize=11, rotate=90)
    page.insert_text((40, 450), "hidden scan words", fontsize=11, render_mode=3)
    page.insert_textbox(pymupdf.Rect(40, 120, 360, 400), "Body text on the page. " * 20, fontsize=11)
    doc.new_page(width=400, height=500).insert_text((40, 80), "Second page text", fontsize=11, color=(0, 0, 1))
    doc.save(path)
    return path


def test_word_layout(poster, tmp_path):
    out = tmp_path / "poster.docx"
    word.convert(str(poster), str(out))  # page layout is the default
    z = zipfile.ZipFile(out)
    document = z.read("word/document.xml").decode()
    assert document.count("<w:sectPr>") == 2, "one section per page"
    assert document.count('w:w="8000" w:h="10000"') == 2, "pages keep their size"
    # page 1 keeps its artwork as a picture behind the text; the all-text page 2 needs none
    assert [n for n in z.namelist() if n.startswith("word/media/")] == ["word/media/image1.jpg"]
    assert 'behindDoc="1"' in document
    text = " ".join(p[2] for p in paragraphs(out))
    assert "Body text on the page." in text and "Second page text" in text
    assert "w:framePr" in document and '<w:color w:val="0000FF"/>' in document, "placed where it was, in its colour"
    for left_in_picture in ("BIG TITLE", "Sideways", "hidden scan words"):
        assert left_in_picture not in text


def test_word_cancel_leaves_no_file(book, tmp_path):
    out = tmp_path / "novel.docx"
    with pytest.raises(epub.Cancelled):
        word.convert(str(book), str(out), progress=lambda done, total: False)
    assert list(tmp_path.glob("novel.docx*")) == []


@pytest.mark.parametrize("label", ["&Word document (page layout)…", "Word document (&reflowable text)…"])
def test_word_export_menu(qtbot, book, tmp_path, monkeypatch, label):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(book))
    out = tmp_path / "from-menu.docx"
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(out), ""))
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == label]
    action.trigger()
    assert out.exists() and "Saved" in viewer.statusBar().currentMessage()
