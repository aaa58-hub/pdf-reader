import zipfile
from xml.etree import ElementTree

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
    word.convert(str(book), str(out))
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


def test_word_cancel_leaves_no_file(book, tmp_path):
    out = tmp_path / "novel.docx"
    with pytest.raises(epub.Cancelled):
        word.convert(str(book), str(out), progress=lambda done, total: False)
    assert list(tmp_path.glob("novel.docx*")) == []


def test_word_export_menu(qtbot, book, tmp_path, monkeypatch):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(book))
    out = tmp_path / "from-menu.docx"
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(out), ""))
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == "&Word document (.docx)…"]
    action.trigger()
    assert out.exists() and "Saved" in viewer.statusBar().currentMessage()
