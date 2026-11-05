import multiprocessing
import sys
from pathlib import Path

import pymupdf

import epub
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressDialog, QScrollArea, QSplitter, QToolBar, QWidget,
)

ZOOM_STEP, ZOOM_MIN, ZOOM_MAX = 1.25, 0.25, 5.0
THUMB_ZOOM = 0.2
GAP = 8  # px around pages
MATCH, CURRENT_MATCH = QColor(255, 220, 0, 90), QColor(255, 120, 0, 130)
DRAG_MIN = 4  # px: a shorter drag is a click, not a selection


def render(page, zoom, dpr=1.0, marks=(), current=None):
    """Page as a QPixmap, with search matches (page-space rects) painted over it."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom * dpr, zoom * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()  # copy: samples die with pix
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)  # sharp text on 125%/150% Windows scaling
    paint(pm, marks, zoom, current)
    return pm


def paint(pm, marks, zoom, current=None):
    if marks:
        painter = QPainter(pm)
        for r in marks:
            painter.fillRect(QRectF(r.x0 * zoom, r.y0 * zoom, r.width * zoom, r.height * zoom),
                             CURRENT_MATCH if r == current else MATCH)
        painter.end()


def nearest_word(words, point):
    return min(range(len(words)), key=lambda k: point.distance_to(pymupdf.Rect(words[k][:4])))


def selected_lines(words, a, b):
    """Words a..b in reading order (like a text selection), one rect per line so highlights have no gaps."""
    lines = {}
    for w in words[min(a, b):max(a, b) + 1]:
        key, r = (w[5], w[6]), pymupdf.Rect(w[:4])  # (block, line)
        lines[key] = lines[key] | r if key in lines else r
    return list(lines.values())


class Viewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Reader")
        self.resize(1100, 800)
        self.doc, self.zoom, self.labels = None, 1.0, []
        self.query, self.matches, self.match = None, [], -1  # matches: [(page index, rect)]
        self.marks = {}  # page index -> rects, for painting
        self.drag = None  # highlight being dragged: (page index, words, first word, press pos, page pixmap)

        self.thumbs = QListWidget()
        self.thumbs.setAccessibleName("Page thumbnails")
        self.thumbs.setIconSize(QSize(100, 130))
        self.thumbs.currentRowChanged.connect(self.go_to)
        self.thumbs.verticalScrollBar().valueChanged.connect(self.schedule_render)

        self.pages = QWidget()
        self.pages.setStyleSheet("background: #c8c8c8")  # page edges stand out
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.pages)
        self.scroll.setAccessibleName("Document")
        self.scroll.verticalScrollBar().valueChanged.connect(self.schedule_render)
        self.scroll.viewport().installEventFilter(self)  # Ctrl+wheel zoom, re-centre pages on resize
        self.pages.installEventFilter(self)  # clicks on pages: annotation tools (QLabel passes them up)
        # Scanned pages take ~0.3s each: render once scrolling pauses, not on every scroll tick.
        self.render_timer = QTimer(self, singleShot=True, interval=60, timeout=self.render_visible)

        self.find_box = QLineEdit(placeholderText="Find in document", clearButtonEnabled=True)
        self.find_box.setAccessibleName("Find in document")
        self.find_box.setMaximumWidth(300)
        self.find_box.returnPressed.connect(self.find_next)
        self.find_box.installEventFilter(self)  # Shift+Enter = previous
        self.find_count = QLabel()
        self.find_bar = QToolBar("Find", movable=False, visible=False)
        self.find_bar.addWidget(self.find_box)
        self.find_bar.addAction("Previous", self.find_prev).setToolTip("Previous match (Shift+F3)")
        self.find_bar.addAction("Next", self.find_next).setToolTip("Next match (F3)")
        self.find_bar.addWidget(self.find_count)
        close = self.find_bar.addAction("\u2715", self.close_find)
        close.setToolTip("Close (Esc)")
        close.setShortcut(Qt.Key_Escape)
        close.setShortcutContext(Qt.WidgetWithChildrenShortcut)  # Esc only while the find bar has focus
        tools = QActionGroup(self)
        tools.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)  # both off = just reading
        tools.triggered.connect(self.tool_changed)
        self.highlight_tool = QAction("&Highlight Text", tools, checkable=True, shortcut="Ctrl+Shift+H",
                                      toolTip="Highlight text: drag across it (Ctrl+Shift+H)")
        self.note_tool = QAction("Add &Note", tools, checkable=True, shortcut="Ctrl+Shift+N",
                                 toolTip="Add a note: click where it goes (Ctrl+Shift+N)")
        annotate_bar = QToolBar("Annotate", movable=False)
        annotate_bar.addActions(tools.actions())
        self.addToolBar(annotate_bar)
        self.addToolBar(self.find_bar)

        split = QSplitter()
        split.addWidget(self.thumbs)
        split.addWidget(self.scroll)
        split.setSizes([160, 940])
        self.setCentralWidget(split)

        for menu_name, items in {
            "&File": [("&Open…", QKeySequence.Open, self.open_dialog),
                      ("&Save", QKeySequence.Save, self.save),
                      ("Save &As…", QKeySequence.SaveAs, self.save_as),
                      ("Export as &EPUB (reflowable text)…", "Ctrl+E", lambda: self.export_epub("reflow")),
                      ("Export as EPUB (&page images)…", "Ctrl+Shift+E", lambda: self.export_epub("fixed")),
                      ("&Quit", QKeySequence.Quit, self.close)],
            "&Edit": [("&Find\u2026", QKeySequence.Find, self.open_find),
                      ("Find &Next", QKeySequence.FindNext, self.find_next),
                      ("Find &Previous", QKeySequence.FindPrevious, self.find_prev)],
            "&View": [("Zoom &In", QKeySequence.ZoomIn, lambda: self.set_zoom(self.zoom * ZOOM_STEP)),
                      ("Zoom &Out", QKeySequence.ZoomOut, lambda: self.set_zoom(self.zoom / ZOOM_STEP)),
                      ("&Actual Size", "Ctrl+0", lambda: self.set_zoom(1.0))],
        }.items():
            menu = self.menuBar().addMenu(menu_name)
            for text, keys, slot in items:
                action = QAction(text, self, shortcut=keys, triggered=slot)
                menu.addAction(action)
        self.menuBar().addMenu("&Annotate").addActions(tools.actions())

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF files (*.pdf)")
        if path:
            self.open(path)

    def keep_changes_or_discard(self):
        """True when the open document may be dropped: no unsaved changes, saved now, or the user says so."""
        if not self.isWindowModified():
            return True
        answer = QMessageBox.question(self, "Unsaved changes", f"Save changes to {Path(self.path).name}?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def closeEvent(self, event):
        if not self.keep_changes_or_discard():
            event.ignore()

    def open(self, path):
        if not self.keep_changes_or_discard():
            return False
        password = None
        try:
            doc = pymupdf.open(path)
            prompt = "This PDF is protected. Password:"
            while doc.needs_pass:  # stays True after unlocking, so stop on authenticate()'s result
                pw, ok = QInputDialog.getText(self, "Password", prompt, QLineEdit.Password)
                if not ok:
                    return False
                if doc.authenticate(pw):
                    password = pw  # the EPUB export reopens the file
                    break
                prompt = "Wrong password. Try again:"
        except Exception as e:
            QMessageBox.critical(self, "Cannot open file", f"{path}\n\n{e}")
            return False
        self.doc, self.path, self.password = doc, path, password
        self.query, self.matches, self.match, self.marks = None, [], -1, {}
        self.drag = None
        self.find_count.clear()
        self.setWindowTitle(f"{Path(path).name}[*] — PDF Reader")  # [*]: Qt shows "*" while modified
        self.setWindowModified(False)
        self.thumbs.clear()
        blank = QPixmap(self.thumbs.iconSize())
        blank.fill(Qt.white)
        for i in range(len(doc)):
            self.thumbs.addItem(QListWidgetItem(QIcon(blank), str(i + 1)))
        self.build_pages()
        return True

    def export_epub(self, mode):
        if not self.doc:
            return
        default = str(Path(self.path).with_suffix(".epub"))
        target, _ = QFileDialog.getSaveFileName(self, "Export as EPUB", default, "EPUB books (*.epub)")
        if not target:
            return
        dialog = QProgressDialog("Converting to EPUB…", "Cancel", 0, 100, self)
        dialog.setWindowTitle("Export as EPUB")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)

        def progress(done, total):
            dialog.setMaximum(total)
            dialog.setValue(done)
            QApplication.processEvents()
            return not dialog.wasCanceled()

        try:
            epub.convert(self.path, target, mode, progress, self.password)
        except epub.Cancelled:
            return
        except Exception as e:
            QMessageBox.critical(self, "Export failed", f"{target}\n\n{e}")
            return
        finally:
            dialog.close()
        self.statusBar().showMessage(f"Saved {target}", 10000)

    def save(self):
        """Append the changes to the open file: fast, and the original bytes stay intact underneath."""
        if not self.doc:
            return False
        if not self.doc.can_save_incrementally():  # e.g. a damaged file MuPDF had to repair
            return self.save_as()
        try:
            self.doc.saveIncr()  # keeps the password, if any
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"{self.path}\n\n{e}")
            return False
        self.setWindowModified(False)
        self.statusBar().showMessage(f"Saved {self.path}", 10000)
        return True

    def save_as(self):
        if not self.doc:
            return False
        target, _ = QFileDialog.getSaveFileName(self, "Save As", self.path, "PDF files (*.pdf)")
        if not target:
            return False
        if Path(target).resolve() == Path(self.path).resolve():
            if self.doc.can_save_incrementally():
                return self.save()
            QMessageBox.warning(self, "Save As", "This file can't be updated in place. Choose a new name.")
            return False
        try:
            self.doc.save(target, encryption=pymupdf.PDF_ENCRYPT_KEEP)  # PyMuPDF's default drops the password
            doc = pymupdf.open(target)  # carry on editing the new file, so Save goes there
            if self.password:
                doc.authenticate(self.password)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"{target}\n\n{e}")
            return False
        self.doc.close()
        self.doc, self.path = doc, target  # same pages: what's on screen stays valid
        self.setWindowTitle(f"{Path(target).name}[*] — PDF Reader")
        self.setWindowModified(False)
        self.statusBar().showMessage(f"Saved {target}", 10000)
        return True

    def tool_changed(self):
        cursor = Qt.IBeamCursor if self.highlight_tool.isChecked() else (
            Qt.CrossCursor if self.note_tool.isChecked() else Qt.ArrowCursor)
        self.pages.setCursor(cursor)

    def at(self, pos):
        """(page index, PDF point) under a point of the pages widget, or None in the gaps between pages."""
        label = self.pages.childAt(pos)
        if not self.doc or label not in self.labels:
            return None
        p = label.mapFromParent(pos)
        return label.index, pymupdf.Point(p.x(), p.y()) / self.zoom * self.doc[label.index].derotation_matrix

    def annot_at(self, i, point):
        page = self.doc[i]  # returned too: an annot is only usable while its page object lives
        return page, next((a for a in reversed(list(page.annots())) if point in a.rect), None)  # topmost

    def page_mouse(self, event):
        """Annotation tools. Returns True when the event was used."""
        kind = event.type()
        if kind == QEvent.ContextMenu:
            hit = self.at(event.pos())
            page, annot = self.annot_at(*hit) if hit else (None, None)
            if annot:
                self.annot_menu(hit[0], page, annot, event.globalPos())
            return bool(annot)
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
            return False
        pos = event.position().toPoint()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            hit = self.at(pos)
            if not hit:
                return False
            i, point = hit
            if self.highlight_tool.isChecked():
                words = self.doc[i].get_text("words")
                if words:
                    self.drag = i, words, nearest_word(words, point), pos, self.labels[i].pixmap()
                return True
            page, annot = self.annot_at(i, point)
            if annot and annot.type[0] == pymupdf.PDF_ANNOT_TEXT:  # click a note to read or edit it
                self.edit_note(i, page, annot)
            elif self.note_tool.isChecked():
                text, ok = QInputDialog.getMultiLineText(self, "Add Note", "Note:")
                if ok and text.strip():
                    page.add_text_annot(point, text)
                    self.changed(i)
            return bool(annot) or self.note_tool.isChecked()
        if not self.drag:
            return False
        i, words, first, start, base = self.drag
        hit = self.at(pos)
        last = nearest_word(words, hit[1]) if hit and hit[0] == i else None
        moved = (pos - start).manhattanLength() >= DRAG_MIN
        if kind == QEvent.MouseMove:
            if last is not None and moved and not base.isNull():  # preview, painted over the page already drawn
                pm = QPixmap(base)
                rot = self.doc[i].rotation_matrix
                paint(pm, [r * rot for r in selected_lines(words, first, last)], self.zoom)
                self.labels[i].setPixmap(pm)
        elif event.button() == Qt.LeftButton:
            self.drag = None
            if last is not None and moved:
                self.doc[i].add_highlight_annot(quads=[r.quad for r in selected_lines(words, first, last)])
                self.changed(i)
            elif not base.isNull():
                self.labels[i].setPixmap(base)
        return True

    def edit_note(self, i, page, annot):
        text, ok = QInputDialog.getMultiLineText(self, "Note", "Note:", annot.info["content"])
        if ok and text != annot.info["content"]:
            annot.set_info(content=text)
            annot.update()
            self.changed(i)

    def annot_menu(self, i, page, annot, global_pos):
        menu = QMenu(self)
        if annot.type[0] == pymupdf.PDF_ANNOT_TEXT:
            menu.addAction("&Edit Note…", lambda: self.edit_note(i, page, annot))
        menu.addAction("&Delete", lambda: (page.delete_annot(annot), self.changed(i)))
        menu.exec(global_pos)

    def changed(self, i):
        """Page i's annotations changed: redraw it and its thumbnail, mark the document unsaved."""
        self.setWindowModified(True)
        self.labels[i].rendered = False
        self.thumbs.item(i).setData(Qt.UserRole, None)
        self.render_visible()

    def build_pages(self):
        for label in self.labels:
            label.deleteLater()
        self.labels = []
        for i, page in enumerate(self.doc):
            label = QLabel(self.pages)
            label.setAccessibleName(f"Page {i + 1}")
            label.index = i
            label.page_size = page.rect.width, page.rect.height
            label.setFixedSize(*self.scaled(label))
            label.setStyleSheet("background: white")
            label.rendered = False
            label.show()  # children added to a visible parent start hidden
            self.labels.append(label)
        self.place_pages()
        self.scroll.verticalScrollBar().setValue(0)
        self.render_visible()

    def place_pages(self):
        """Stack pages by hand: Qt layouts cap at 524287px total, which a long book passes when zoomed in."""
        # ponytail: QWidget itself caps at 16777215px (~4000 pages at 500%); page virtually if that's ever hit
        viewport = self.scroll.viewport()
        width = max([l.width() for l in self.labels] + [0]) + 2 * GAP
        width = max(width, viewport.width())
        y = GAP
        for label in self.labels:
            label.move((width - label.width()) // 2, y)
            y += label.height() + GAP
        self.pages.resize(width, max(y, viewport.height()))

    def set_zoom(self, zoom, anchor_y=0):
        """Zoom keeping the document point at viewport height anchor_y still (the mouse, for Ctrl+wheel)."""
        if not self.doc:
            return
        bar = self.scroll.verticalScrollBar()
        y = bar.value() + anchor_y
        i = self.page_at(y)
        frac = max(0, (y - self.labels[i].y()) / self.labels[i].height())  # in the gap above: pin to page top
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        for label in self.labels:
            label.setFixedSize(*self.scaled(label))
            label.clear()
            label.rendered = False
        self.place_pages()
        bar.setValue(round(self.labels[i].y() + frac * self.labels[i].height() - anchor_y))
        self.schedule_render()  # a touchpad pinch sends many events: draw once it settles

    def scaled(self, label):
        w, h = label.page_size
        return round(w * self.zoom), round(h * self.zoom)

    def schedule_render(self):
        self.render_timer.start()

    def eventFilter(self, obj, event):
        if obj is self.pages:
            return self.page_mouse(event) or super().eventFilter(obj, event)
        if event.type() == QEvent.Resize:
            self.place_pages()
            self.schedule_render()
        elif (obj is self.find_box and event.type() == QEvent.KeyPress
              and event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ShiftModifier):
            self.find_prev()
            return True
        elif event.type() == QEvent.Wheel and event.modifiers() & Qt.ControlModifier:
            # 120 = one mouse-wheel notch; touchpads send smaller steps
            self.set_zoom(self.zoom * ZOOM_STEP ** (event.angleDelta().y() / 120), event.position().y())
            return True
        return super().eventFilter(obj, event)

    def page_at(self, y):
        return next((i for i, l in enumerate(self.labels) if l.geometry().bottom() >= y), len(self.labels) - 1)

    def current_page(self):
        return self.page_at(self.scroll.verticalScrollBar().value())

    def go_to(self, index):
        if 0 <= index < len(self.labels):
            self.scroll.verticalScrollBar().setValue(self.labels[index].y() - GAP)
            self.render_visible()

    def render_visible(self):
        """Draw one missing page or thumbnail per call, pages first, then yield to the event loop.

        A scanned page costs ~0.3s whatever the zoom, so drawing a batch at once freezes the window.
        Off-screen pages are freed, so memory stays flat on big files.
        """
        self.follow_in_sidebar()
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        todo = []
        for i, label in enumerate(self.labels):
            visible = label.y() <= bottom and label.y() + label.height() >= top
            if visible and not label.rendered:
                todo.append(("page", i))
            elif not visible and label.rendered:
                label.clear()
                label.rendered = False
        view = self.thumbs.viewport().rect()
        for i in range(self.thumbs.count()):
            item = self.thumbs.item(i)
            if not item.data(Qt.UserRole) and self.thumbs.visualItemRect(item).intersects(view):
                todo.append(("thumb", i))
        if not todo:
            return
        kind, i = todo[0]
        dpr = self.devicePixelRatioF()
        if kind == "page":
            current = self.matches[self.match][1] if self.match >= 0 else None
            self.labels[i].setPixmap(render(self.doc[i], self.zoom, dpr, self.marks.get(i, ()), current))
            self.labels[i].rendered = True
        else:
            self.thumbs.item(i).setIcon(QIcon(render(self.doc[i], THUMB_ZOOM, dpr)))
            self.thumbs.item(i).setData(Qt.UserRole, True)  # thumbnails are small: keep them once drawn
        QTimer.singleShot(1, self.render_visible)  # 1ms, not 0: lets timers and input run in between

    def open_find(self):
        self.find_bar.show()
        self.find_box.setFocus()
        self.find_box.selectAll()

    def close_find(self):
        self.find_bar.hide()
        self.find_box.clear()
        self.search("")
        self.scroll.setFocus()

    def search(self, text):
        self.query, self.matches, self.match = text, [], -1
        if text and self.doc:
            QApplication.setOverrideCursor(Qt.WaitCursor)  # ~1s for a 572-page book
            try:
                for i, page in enumerate(self.doc):
                    self.matches += [(i, r * page.rotation_matrix) for r in page.search_for(text)]
            finally:
                QApplication.restoreOverrideCursor()
        self.marks = {}
        for i, r in self.matches:
            self.marks.setdefault(i, []).append(r)
        if self.matches:  # start from the page being read, like other viewers
            start = self.current_page()
            self.match = next((k for k, (i, _) in enumerate(self.matches) if i >= start), 0)
        self.show_match()

    def find_next(self):
        self.step_match(1)

    def find_prev(self):
        self.step_match(-1)

    def step_match(self, step):
        text = self.find_box.text()
        if not self.find_bar.isVisible() or text != self.query:
            self.find_bar.show()
            return self.search(text)
        if self.matches:
            self.match = (self.match + step) % len(self.matches)
        self.show_match()

    def show_match(self):
        if self.matches:
            self.find_count.setText(f"  {self.match + 1} of {len(self.matches)}  ")
        else:
            self.find_count.setText("  No results  " if self.query else "")
        for label in self.labels:  # repaint highlights (only on-screen pages are drawn anyway)
            label.clear()
            label.rendered = False
        if self.matches:
            i, r = self.matches[self.match]
            label = self.labels[i]
            # scroll only if the match is off-screen, then leave it a third of the way down
            self.scroll.ensureVisible(round(label.x() + r.x0 * self.zoom), round(label.y() + r.y0 * self.zoom),
                                      50, self.scroll.viewport().height() // 3)
        self.render_visible()

    def follow_in_sidebar(self):
        """Highlight the current page's thumbnail and scroll it into view, without jumping the document."""
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        # the page filling most of the view, not a sliver left at the top; ties go to the earlier page
        shown = [min(bottom, l.y() + l.height()) - max(top, l.y()) for l in self.labels]
        page = shown.index(max(shown)) if shown else 0
        if self.labels and self.thumbs.currentRow() != page:
            self.thumbs.blockSignals(True)  # else currentRowChanged -> go_to snaps the view to the page top
            self.thumbs.setCurrentRow(page)
            self.thumbs.blockSignals(False)
            self.thumbs.scrollToItem(self.thumbs.item(page))

def main():
    app = QApplication(sys.argv)
    viewer = Viewer()
    viewer.show()
    if len(sys.argv) > 1:
        viewer.open(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    multiprocessing.freeze_support()  # the .exe re-launches itself for EPUB page-rendering workers
    main()
