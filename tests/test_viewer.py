import pymupdf
import pytest
from PySide6.QtGui import QAction

from pdf_reader import Viewer


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    for i in range(30):
        doc.new_page(width=600, height=800).insert_text((72, 72), f"Page {i + 1}")
    doc.save(path)
    return path


@pytest.fixture
def viewer(qtbot):
    v = Viewer()
    qtbot.addWidget(v)
    v.show()
    return v


def rendered(v):
    return [i for i, label in enumerate(v.labels) if label.rendered]


def test_open_shows_pages_and_thumbnails(viewer, pdf, qtbot):
    assert viewer.open(str(pdf))
    assert viewer.thumbs.count() == 30
    assert len(viewer.labels) == 30
    assert "sample.pdf" in viewer.windowTitle()
    qtbot.waitUntil(lambda: 0 in rendered(viewer))
    assert 29 not in rendered(viewer), "off-screen pages must not be rendered"


def test_thumbnail_click_scrolls_to_page(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    qtbot.waitUntil(lambda: 0 in rendered(viewer))
    viewer.thumbs.setCurrentRow(20)
    assert viewer.current_page() == 20
    assert 20 in rendered(viewer)
    assert 0 not in rendered(viewer), "scrolled-away pages are freed"


def test_zoom_resizes_pages_and_keeps_position(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    viewer.thumbs.setCurrentRow(10)
    width = viewer.labels[0].width()
    viewer.set_zoom(2.0)
    assert viewer.labels[0].width() == width * 2
    qtbot.waitUntil(lambda: viewer.current_page() == 10)
    viewer.set_zoom(100)
    assert viewer.zoom == 5.0, "zoom is clamped"


def test_zoom_shortcut(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    [zoom_in] = [a for a in viewer.findChildren(QAction) if a.text() == "Zoom &In"]
    zoom_in.trigger()
    assert viewer.zoom == 1.25


def test_bad_file_shows_error(viewer, tmp_path, monkeypatch):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    errors = []
    monkeypatch.setattr("pdf_reader.QMessageBox.critical", lambda *a: errors.append(a))
    assert not viewer.open(str(bad))
    assert errors and viewer.doc is None


def test_password_protected(viewer, tmp_path, monkeypatch):
    path = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    answers = iter([("wrong", True), ("secret", True)])
    monkeypatch.setattr("pdf_reader.QInputDialog.getText", lambda *a: next(answers))
    assert viewer.open(str(path))
    assert len(viewer.labels) == 1
