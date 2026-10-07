import re
import zipfile

import pymupdf
import pytest
from PySide6.QtGui import QAction

import epub
from pdf_reader import Viewer

BODY = ("It was a bright cold day in April, and the clocks were striking thirteen. The hallway smelt of "
        "boiled cabbage and old rag mats. At one end of it a coloured poster, too large for indoor display, "
        "had been tacked to the wall.")


@pytest.fixture
def book(tmp_path):
    """4 pages: two chapters, a running head, page numbers, a figure, a sentence split by a page turn."""
    path = tmp_path / "novel.pdf"
    doc = pymupdf.open()
    figure = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80), False)
    figure.set_rect(figure.irect, (200, 30, 30))
    for n, (heading, text) in enumerate([("Chapter One", BODY), (None, BODY + " Winston made for the"),
                                         (None, "stairs, slowly, resting several times."), ("Chapter Two", BODY)]):
        page = doc.new_page(width=420, height=640)
        page.insert_text((150, 30), "THE NOVEL", fontsize=9)  # running head
        page.insert_text((205, 620), str(n + 1), fontsize=9)  # page number
        y = 60
        if heading:
            page.insert_text((60, 110), heading, fontsize=22)
            y = 140
        if n == 1:
            page.insert_image(pymupdf.Rect(150, 60, 270, 140), pixmap=figure)
            y = 160
        page.insert_textbox(pymupdf.Rect(50, y, 370, 600), text, fontsize=11)
    doc.set_metadata({"title": "The Novel", "author": "A. Writer"})
    doc.save(path)
    return path


def read(path):
    z = zipfile.ZipFile(path)
    text = "".join(z.read(n).decode() for n in sorted(z.namelist()) if n.startswith("OEBPS/text/"))
    return z, text


def test_reflow_structure(book, tmp_path):
    out = tmp_path / "novel.epub"
    epub.convert(str(book), str(out))
    z, text = read(out)
    first = z.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED, "EPUB rule: mimetype first"
    opf = z.read("OEBPS/content.opf").decode()
    assert "<dc:title>The Novel</dc:title>" in opf and "A. Writer" in opf and "<dc:language>en<" in opf
    assert 'properties="cover-image"' in opf
    nav = z.read("OEBPS/nav.xhtml").decode()
    assert re.findall(r">([^<]+)</a>", nav) == ["Chapter One", "Chapter Two"]
    assert "THE NOVEL" not in text, "running head dropped"
    assert not re.search(r"<p>\d</p>", text), "page numbers dropped"
    assert "Winston made for the stairs, slowly" in text, "sentence split by the page turn is rejoined"
    assert text.count("<img") == 1, "figure kept"


def test_fixed_layout(book, tmp_path):
    out = tmp_path / "novel.epub"
    epub.convert(str(book), str(out), "fixed")
    z, text = read(out)
    opf = z.read("OEBPS/content.opf").decode()
    assert "pre-paginated" in opf
    assert text.count("<img") == 4 and 'name="viewport"' in text
    assert all(z.read(n)[:2] == b"\xff\xd8" for n in z.namelist() if n.startswith("OEBPS/images/")), "JPEGs"


def test_cancel_leaves_no_file(book, tmp_path):
    out = tmp_path / "novel.epub"
    with pytest.raises(epub.Cancelled):
        epub.convert(str(book), str(out), progress=lambda done, total: False)
    assert list(tmp_path.glob("novel.epub*")) == []


def test_password_protected(book, tmp_path):
    locked = tmp_path / "locked.pdf"
    pymupdf.open(book).save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    out = tmp_path / "locked.epub"
    epub.convert(str(locked), str(out), "fixed", password="pw")
    assert read(out)[1].count("<img") == 4


def test_language_detection():
    assert epub.detect_language("il gatto e il cane che non sono della casa per la") == "it"
    assert epub.detect_language("the cat and the dog that is in the house") == "en"


def test_export_menu(qtbot, book, tmp_path, monkeypatch):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(book))
    out = tmp_path / "from-menu.epub"
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(out), ""))
    [action] = [a for a in viewer.findChildren(QAction) if a.text().startswith("Export as &EPUB")]
    action.trigger()
    assert out.exists() and "Saved" in viewer.statusBar().currentMessage()
