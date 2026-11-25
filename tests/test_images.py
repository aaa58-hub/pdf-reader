import pymupdf
import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog

import epub
import images
from pdf_reader import PagesDialog, Viewer
from test_epub import book  # noqa: F401  (the 4-page novel fixture: 420 x 640 pt pages)


@pytest.fixture
def out(tmp_path):
    """An empty folder for the images (the fixture PDF sits in tmp_path)."""
    folder = tmp_path / "out"
    folder.mkdir()
    return folder


def test_page_range_as_png(book, out):
    saved = images.convert(str(book), str(out / "novel.png"), 1, 2, 72)
    assert sorted(p.name for p in out.iterdir()) == ["novel-2.png", "novel-3.png"], "named by page number"
    pix = pymupdf.Pixmap(str(out / "novel-2.png"))
    assert (pix.width, pix.height) == (420, 640), "72 dpi: one pixel per point"
    assert saved == f"2 images in {out}"


def test_one_page_as_jpeg_at_print_resolution(book, out):
    cover = out / "cover.jpg"
    assert images.convert(str(book), str(cover), 0, 0, 300) == str(cover), "a single page keeps the chosen name"
    assert cover.read_bytes()[:3] == b"\xff\xd8\xff", "JPEG"
    assert pymupdf.Pixmap(str(cover)).width == 1750  # 420 pt at 300 dpi
    assert [p.name for p in out.iterdir()] == ["cover.jpg"]


def test_cancel_leaves_no_half_written_file(book, out):
    with pytest.raises(epub.Cancelled):
        images.convert(str(book), str(out / "novel.png"), 0, 3, 72, progress=lambda done, total: False)
    assert not list(out.glob("*.part"))


def test_typing_the_last_page_leaves_the_first_alone(qtbot):
    dialog = PagesDialog(None, 40, "Export pages as images", resolution=True)
    qtbot.addWidget(dialog)
    assert (dialog.pages(), dialog.dpi.value()) == ((1, 40), 150), "all pages, screen dpi"
    dialog.first.setValue(5)
    dialog.last.lineEdit().selectAll()
    qtbot.keyClicks(dialog.last, "12")  # passes through "1" on the way
    assert dialog.pages() == (5, 12)
    dialog.last.setValue(3)
    assert dialog.pages() == (3, 5), "a range typed backwards is read the right way round"


def test_dialog_says_each_page_is_its_own_file(qtbot):
    dialog = PagesDialog(None, 40, "Export pages as images", resolution=True)
    qtbot.addWidget(dialog)
    dialog.last.setValue(20)
    assert dialog.files.text().startswith("20 image files: each page is saved as its own file")
    dialog.first.setValue(20)
    assert dialog.files.text() == "1 image file"


def test_highlights_stay_note_icons_go(tmp_path, out):
    pdf = tmp_path / "notes.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 50), "Highlighted words", fontsize=14)
    page.add_highlight_annot(page.search_for("Highlighted"))
    page.add_text_annot((200, 100), "A note")
    doc.save(pdf)
    images.convert(str(pdf), str(out / "page.png"), 0, 0, 72)
    pix = pymupdf.Pixmap(str(out / "page.png"))
    assert pix.pixel(25, 47)[2] < 150, "highlight painted (yellow: little blue)"
    assert all(pix.pixel(x, y) == (255, 255, 255) for x in range(200, 216) for y in range(100, 116)), "no note icon"


def test_export_images_menu(qtbot, book, out, monkeypatch):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(book))

    def choose_pages_3_to_4(dialog):
        dialog.first.setValue(3)
        dialog.dpi.setValue(72)
        return QDialog.Accepted

    monkeypatch.setattr(PagesDialog, "exec", choose_pages_3_to_4)
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(out / "page.png"), ""))
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == "Pages as &images…"]
    action.trigger()
    assert sorted(p.name for p in out.iterdir()) == ["page-3.png", "page-4.png"]
    assert viewer.statusBar().currentMessage() == f"Saved 2 images in {out}"
