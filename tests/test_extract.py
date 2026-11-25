import pymupdf
import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog

import extract
from pdf_reader import PagesDialog, Viewer


@pytest.fixture
def report(tmp_path):
    """6 pages, two parts with a sub-bookmark each, a highlight and two links on page 4."""
    path = tmp_path / "report.pdf"
    doc = pymupdf.open()
    for n in range(1, 7):
        doc.new_page(width=300, height=400).insert_text((50, 60), f"Page {n} text", fontsize=12)
    doc.set_toc([[1, "Part A", 1], [2, "Sub A1", 2], [1, "Part B", 4], [2, "Sub B1", 5]])
    page = doc[3]
    page.add_highlight_annot(page.search_for("Page 4"))
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 100, 150, 120), "uri": "https://example.com"})
    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(50, 140, 150, 160), "page": 4})
    doc.set_metadata({"title": "Annual report", "author": "A. Writer"})
    doc.save(path)
    return path


def test_page_range_keeps_text_annotations_links_and_bookmarks(report, tmp_path):
    out = tmp_path / "part-b.pdf"
    extract.convert(str(report), str(out), 2, 4)
    doc = pymupdf.open(out)
    assert [p.get_text().strip() for p in doc] == ["Page 3 text", "Page 4 text", "Page 5 text"]
    assert [a.type[1] for a in doc[1].annots()] == ["Highlight"]
    assert [link.get("uri") or link["page"] for link in doc[1].get_links()] == ["https://example.com", 2], \
        "a link to page 5 now points to page 5's new place (index 2)"
    assert doc.get_toc() == [[1, "Part B", 2], [2, "Sub B1", 3]], "bookmarks outside the range are dropped"
    assert doc.metadata["title"] == "Annual report" and doc.metadata["author"] == "A. Writer"


def test_orphan_sub_bookmark_moves_up(report, tmp_path):
    out = tmp_path / "page-2.pdf"
    extract.convert(str(report), str(out), 1, 1)
    assert pymupdf.open(out).get_toc() == [[1, "Sub A1", 1]]


def test_protected_pdf_stays_protected(report, tmp_path):
    locked = tmp_path / "locked.pdf"
    pymupdf.open(report).save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    out = tmp_path / "locked-part.pdf"
    extract.convert(str(locked), str(out), 0, 1, password="secret")
    doc = pymupdf.open(out)
    assert doc.needs_pass and doc.authenticate("secret") and len(doc) == 2


def test_refuses_to_overwrite_the_open_pdf(report):
    with pytest.raises(ValueError):
        extract.convert(str(report), str(report), 0, 0)
    assert len(pymupdf.open(report)) == 6


def test_export_pages_menu(qtbot, report, tmp_path, monkeypatch):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(report))

    def choose_pages_2_to_3(dialog):
        assert dialog.windowTitle() == "Save pages as a new PDF" and dialog.dpi.isHidden(), "no resolution for a PDF"
        dialog.first.setValue(2)
        dialog.last.setValue(3)
        return QDialog.Accepted

    offered = []
    out = tmp_path / "chosen.pdf"

    def save_dialog(parent, caption, default, file_filter):
        offered.append(default)
        return str(out), ""

    monkeypatch.setattr(PagesDialog, "exec", choose_pages_2_to_3)
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", save_dialog)
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == "Pages as a new P&DF…"]
    action.trigger()
    assert offered == [str(tmp_path / "report (pages 2-3).pdf")], "suggested name says which pages"
    assert len(pymupdf.open(out)) == 2 and viewer.statusBar().currentMessage() == f"Saved {out}"
