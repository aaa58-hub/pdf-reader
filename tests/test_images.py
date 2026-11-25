import pymupdf
import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog

import epub
import images
from pdf_reader import PageImagesDialog, Viewer
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


def test_dialog_keeps_the_range_the_right_way_round(qtbot):
    dialog = PageImagesDialog(None, 10)
    qtbot.addWidget(dialog)
    assert (dialog.first.value(), dialog.last.value(), dialog.dpi.value()) == (1, 10, 150), "all pages, screen dpi"
    dialog.last.setValue(4)
    dialog.first.setValue(7)
    assert dialog.last.value() == 7
    dialog.last.setValue(2)
    assert dialog.first.value() == 2


def test_export_images_menu(qtbot, book, out, monkeypatch):
    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.open(str(book))

    def choose_pages_3_to_4(dialog):
        dialog.first.setValue(3)
        dialog.dpi.setValue(72)
        return QDialog.Accepted

    monkeypatch.setattr(PageImagesDialog, "exec", choose_pages_3_to_4)
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(out / "page.png"), ""))
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == "Pages as &images…"]
    action.trigger()
    assert sorted(p.name for p in out.iterdir()) == ["page-3.png", "page-4.png"]
    assert viewer.statusBar().currentMessage() == f"Saved 2 images in {out}"
