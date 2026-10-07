import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QWheelEvent
from PySide6.QtWidgets import QApplication

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
    qtbot.waitUntil(lambda: 20 in rendered(viewer))
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


def thumbs_rendered(v):
    return [i for i in range(v.thumbs.count()) if v.thumbs.item(i).data(Qt.UserRole)]


def test_thumbnails_render_lazily(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    qtbot.waitUntil(lambda: 0 in thumbs_rendered(viewer))
    assert 29 not in thumbs_rendered(viewer), "off-screen thumbnails must not be rendered on open"
    viewer.thumbs.scrollToBottom()
    qtbot.waitUntil(lambda: 29 in thumbs_rendered(viewer))


def ctrl_wheel(viewer, notches):
    vp = viewer.scroll.viewport()
    pos = QPointF(vp.rect().center())
    event = QWheelEvent(pos, vp.mapToGlobal(pos), QPoint(), QPoint(0, 120 * notches),
                        Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(vp, event)


def test_ctrl_wheel_zooms(viewer, pdf):
    viewer.open(str(pdf))
    ctrl_wheel(viewer, 1)
    assert viewer.zoom == pytest.approx(1.25)
    ctrl_wheel(viewer, -2)
    assert viewer.zoom == pytest.approx(0.8)


def test_plain_wheel_scrolls_without_zoom(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    qtbot.waitUntil(lambda: 0 in rendered(viewer))
    vp = viewer.scroll.viewport()
    pos = QPointF(vp.rect().center())
    QApplication.sendEvent(vp, QWheelEvent(pos, vp.mapToGlobal(pos), QPoint(), QPoint(0, -120),
                                           Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
    assert viewer.zoom == 1.0
    assert viewer.scroll.verticalScrollBar().value() > 0


def test_repeated_zoom_stays_on_page(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    viewer.thumbs.setCurrentRow(20)
    for _ in range(3):
        ctrl_wheel(viewer, 1)
    assert viewer.current_page() == 20
    ctrl_wheel(viewer, -3)
    assert viewer.current_page() == 20
    qtbot.waitUntil(lambda: 20 in rendered(viewer))


def test_long_document_zoomed_in_pages_do_not_overlap(viewer, tmp_path):
    # 150 pages x 800pt at 500% = ~600k px: past Qt layouts' 524287px cap that made pages overlap
    path = tmp_path / "long.pdf"
    doc = pymupdf.open()
    for _ in range(150):
        doc.new_page(width=600, height=800)
    doc.save(path)
    viewer.open(str(path))
    viewer.set_zoom(5.0)
    labels = viewer.labels
    assert all(a.y() + a.height() < b.y() for a, b in zip(labels, labels[1:]))
    assert viewer.pages.height() > labels[-1].y() + labels[-1].height()


def test_sidebar_follows_scrolled_page(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    bar = viewer.scroll.verticalScrollBar()
    target = viewer.labels[15].y() + 100  # partway into page 16
    bar.setValue(target)
    qtbot.waitUntil(lambda: viewer.thumbs.currentRow() == 15)
    assert bar.value() == target, "highlighting the thumbnail must not snap the document"
    item_rect = viewer.thumbs.visualItemRect(viewer.thumbs.item(15))
    assert viewer.thumbs.viewport().rect().intersects(item_rect), "current thumbnail scrolled into view"


def find(viewer, qtbot, text):
    viewer.open_find()
    viewer.find_box.setText(text)
    qtbot.keyClick(viewer.find_box, Qt.Key_Return)


def test_ctrl_f_opens_find_bar(viewer, pdf):
    viewer.open(str(pdf))
    [action] = [a for a in viewer.findChildren(QAction) if a.text() == "&Find…"]
    action.trigger()
    assert viewer.find_bar.isVisible()


def test_search_counts_and_steps_through_matches(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    find(viewer, qtbot, "Page 2")  # pages 2 and 20-29 contain it
    assert len(viewer.matches) == 11
    assert viewer.find_count.text().strip() == "1 of 11"
    qtbot.keyClick(viewer.find_box, Qt.Key_Return)
    assert viewer.matches[viewer.match][0] == 19, "Enter goes to the next match (page 20)"
    i, r = viewer.matches[viewer.match]
    top = viewer.scroll.verticalScrollBar().value()
    assert top < viewer.labels[i].y() + r.y0 < top + viewer.scroll.viewport().height(), "match scrolled into view"
    qtbot.keyClick(viewer.find_box, Qt.Key_Return, Qt.ShiftModifier)
    qtbot.keyClick(viewer.find_box, Qt.Key_Return, Qt.ShiftModifier)
    assert viewer.find_count.text().strip() == "11 of 11", "Shift+Enter goes back and wraps"


def test_search_starts_from_current_page(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    viewer.go_to(24)
    find(viewer, qtbot, "Page 2")
    assert viewer.matches[viewer.match][0] == 24


def test_matches_are_highlighted(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    find(viewer, qtbot, "Page 1")
    qtbot.waitUntil(lambda: 0 in rendered(viewer))
    r = viewer.matches[0][1]
    pixel = viewer.labels[0].pixmap().toImage().pixelColor(int(r.x0 + 2), int(r.y0 + 2))
    assert pixel.blue() < 200 < pixel.red(), "current match tinted orange"


def test_no_results_and_close(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    find(viewer, qtbot, "zebra")
    assert viewer.find_count.text().strip() == "No results"
    viewer.close_find()
    assert not viewer.find_bar.isVisible() and viewer.matches == []


def test_opening_another_file_resets_search(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    find(viewer, qtbot, "Page")
    viewer.open(str(pdf))
    assert viewer.matches == [] and viewer.find_count.text() == ""


def test_sidebar_ignores_sliver_of_previous_page(viewer, pdf, qtbot):
    viewer.open(str(pdf))
    viewer.scroll.verticalScrollBar().setValue(viewer.labels[6].y() - 40)  # 40px of page 6 still showing
    qtbot.waitUntil(lambda: viewer.thumbs.currentRow() == 6)
