import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from pdf_reader import Viewer

LINE1, LINE2 = "The quick brown fox jumps", "over the lazy dog today"


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "notes.pdf"
    doc = pymupdf.open()
    for rotation in (0, 90):
        page = doc.new_page(width=400, height=300)
        page.insert_text((50, 50), LINE1, fontsize=14)
        page.insert_text((50, 80), LINE2, fontsize=14)
        page.set_rotation(rotation)
    doc.save(path)
    return path


@pytest.fixture
def viewer(qtbot, pdf):
    v = Viewer()
    qtbot.addWidget(v, before_close_func=lambda w: w.setWindowModified(False))  # no "save changes?" box
    v.show()
    v.open(str(pdf))
    qtbot.waitUntil(lambda: v.labels[0].rendered)
    return v


def screen_pos(viewer, i, word):
    """Middle of a word on page i, in that page label's pixels."""
    page = viewer.doc[i]
    r = next(pymupdf.Rect(w[:4]) for w in page.get_text("words") if w[4] == word) * page.rotation_matrix
    return QPoint(round((r.x0 + r.x1) / 2 * viewer.zoom), round((r.y0 + r.y1) / 2 * viewer.zoom))


def mouse(widget, kind, pos, button=Qt.LeftButton):
    buttons = Qt.NoButton if kind == QEvent.MouseButtonRelease else button
    QApplication.sendEvent(widget, QMouseEvent(kind, QPointF(pos), widget.mapToGlobal(QPointF(pos)),
                                               button, buttons, Qt.NoModifier))


def drag(viewer, i, from_word, to_word):
    label = viewer.labels[i]
    a, b = screen_pos(viewer, i, from_word), screen_pos(viewer, i, to_word)
    mouse(label, QEvent.MouseButtonPress, a)
    mouse(label, QEvent.MouseMove, b)
    mouse(label, QEvent.MouseButtonRelease, b)


def annots(viewer, i):
    """(type, text, quads) per annotation, read while the page object lives: annots die with their page."""
    page = viewer.doc[i]
    return [(a.type[0], a.info["content"], quads(a)) for a in page.annots()]


@pytest.mark.parametrize("i", [0, 1], ids=["upright", "rotated"])
def test_drag_highlights_words_in_reading_order(viewer, i):
    viewer.highlight_tool.trigger()
    drag(viewer, i, "brown", "lazy")
    [(kind, _, marked)] = annots(viewer, i)
    assert kind == pymupdf.PDF_ANNOT_HIGHLIGHT
    covered = " ".join(w[4] for w in viewer.doc[i].get_text("words")
                       if any(pymupdf.Rect(w[:4]).intersects(q.rect) for q in marked))
    assert covered == "brown fox jumps over the lazy", "a text selection, not a rectangle"
    assert viewer.isWindowModified()


def quads(annot):
    v = annot.vertices or []  # notes have none
    return [pymupdf.Quad(v[k:k + 4]) for k in range(0, len(v), 4)]


def test_click_without_drag_adds_nothing(viewer):
    viewer.highlight_tool.trigger()
    pos = screen_pos(viewer, 0, "fox")
    mouse(viewer.labels[0], QEvent.MouseButtonPress, pos)
    mouse(viewer.labels[0], QEvent.MouseButtonRelease, pos)
    assert annots(viewer, 0) == [] and not viewer.isWindowModified()


def test_no_tool_no_annotation(viewer):
    drag(viewer, 0, "quick", "jumps")
    assert annots(viewer, 0) == []


def test_add_and_edit_note(viewer, monkeypatch):
    answers = iter([("Check this date", True), ("Checked", True)])
    seen = []
    monkeypatch.setattr("pdf_reader.QInputDialog.getMultiLineText",
                        lambda parent, title, label, text="": seen.append(text) or next(answers))
    viewer.note_tool.trigger()
    pos = screen_pos(viewer, 0, "dog")
    mouse(viewer.labels[0], QEvent.MouseButtonPress, pos)
    assert [a[:2] for a in annots(viewer, 0)] == [(pymupdf.PDF_ANNOT_TEXT, "Check this date")]

    viewer.note_tool.trigger()  # tool off: clicking the note opens it
    assert not viewer.note_tool.isChecked()
    page = viewer.doc[0]
    icon = page.first_annot.rect * page.rotation_matrix * viewer.zoom
    mouse(viewer.labels[0], QEvent.MouseButtonPress, QPoint(round(icon.x0) + 3, round(icon.y0) + 3))
    assert seen[-1] == "Check this date", "the dialog shows the current text"
    assert annots(viewer, 0)[0][1] == "Checked"


def choose_in_menu(text):
    menu = QApplication.activePopupWidget()
    next(a for a in menu.actions() if a.text() == text).trigger()
    menu.close()


def test_delete_from_right_click_menu(viewer):
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    QTimer.singleShot(0, lambda: choose_in_menu("&Delete"))  # runs inside the menu's own event loop
    pos = screen_pos(viewer, 0, "brown")
    QApplication.sendEvent(viewer.labels[0], QContextMenuEvent(QContextMenuEvent.Mouse, pos,
                                                               viewer.labels[0].mapToGlobal(pos)))
    assert annots(viewer, 0) == []


def test_save_keeps_annotations_and_clears_star(viewer, pdf):
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    assert viewer.save()
    assert not viewer.isWindowModified()
    assert len(list(pymupdf.open(pdf)[0].annots())) == 1


def test_save_as_writes_new_file_and_keeps_editing_it(viewer, pdf, tmp_path, monkeypatch):
    copy = tmp_path / "copy.pdf"
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(copy), ""))
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    assert viewer.save_as()
    assert "copy.pdf" in viewer.windowTitle()
    assert len(list(pymupdf.open(copy)[0].annots())) == 1
    assert len(list(pymupdf.open(pdf)[0].annots())) == 0, "original untouched"
    drag(viewer, 0, "over", "dog")
    viewer.save()
    assert len(list(pymupdf.open(copy)[0].annots())) == 2


def test_save_keeps_password(viewer, tmp_path, monkeypatch):
    locked = tmp_path / "locked.pdf"
    pymupdf.open(viewer.path).save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="o")
    monkeypatch.setattr("pdf_reader.QInputDialog.getText", lambda *a: ("pw", True))
    viewer.open(str(locked))
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    copy = tmp_path / "locked-copy.pdf"
    monkeypatch.setattr("pdf_reader.QFileDialog.getSaveFileName", lambda *a: (str(copy), ""))
    assert viewer.save_as()
    for path in (copy,):
        doc = pymupdf.open(path)
        assert doc.needs_pass and doc.authenticate("pw") and len(list(doc[0].annots())) == 1


@pytest.mark.parametrize("answer, stays", [(QMessageBox.Cancel, True), (QMessageBox.Discard, False)])
def test_unsaved_changes_prompt(viewer, pdf, tmp_path, monkeypatch, answer, stays):
    other = tmp_path / "other.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(other)
    monkeypatch.setattr("pdf_reader.QMessageBox.question", lambda *a: answer)
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    viewer.open(str(other))
    assert (viewer.path == str(pdf)) == stays
    assert len(list(pymupdf.open(pdf)[0].annots())) == 0, "discard never writes"


def test_close_with_unsaved_changes_can_be_cancelled(viewer, monkeypatch):
    monkeypatch.setattr("pdf_reader.QMessageBox.question", lambda *a: QMessageBox.Cancel)
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    viewer.close()
    assert viewer.isVisible()


def test_highlight_shows_on_page(viewer, qtbot):
    viewer.highlight_tool.trigger()
    drag(viewer, 0, "quick", "fox")
    qtbot.waitUntil(lambda: viewer.labels[0].rendered)
    pos = screen_pos(viewer, 0, "brown")
    img = viewer.labels[0].pixmap().toImage()
    dpr = img.devicePixelRatio()
    pixels = [img.pixelColor(round((pos.x() + dx) * dpr), round((pos.y() + 6) * dpr)) for dx in range(-10, 10)]
    assert any(p.blue() < 150 < p.red() and p.green() > 150 for p in pixels), "yellow highlight drawn"
