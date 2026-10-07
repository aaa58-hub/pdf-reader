import sys
from pathlib import Path

import pymupdf
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QScrollArea, QSplitter, QToolBar, QWidget,
)

ZOOM_STEP, ZOOM_MIN, ZOOM_MAX = 1.25, 0.25, 5.0
THUMB_ZOOM = 0.2
GAP = 8  # px around pages
MATCH, CURRENT_MATCH = QColor(255, 220, 0, 90), QColor(255, 120, 0, 130)


def render(page, zoom, dpr=1.0, marks=(), current=None):
    """Page as a QPixmap, with search matches (page-space rects) painted over it."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom * dpr, zoom * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()  # copy: samples die with pix
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)  # sharp text on 125%/150% Windows scaling
    if marks:
        painter = QPainter(pm)
        for r in marks:
            painter.fillRect(QRectF(r.x0 * zoom, r.y0 * zoom, r.width * zoom, r.height * zoom),
                             CURRENT_MATCH if r == current else MATCH)
        painter.end()
    return pm


class Viewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Reader")
        self.resize(1100, 800)
        self.doc, self.zoom, self.labels = None, 1.0, []
        self.query, self.matches, self.match = None, [], -1  # matches: [(page index, rect)]
        self.marks = {}  # page index -> rects, for painting

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
        self.addToolBar(self.find_bar)

        split = QSplitter()
        split.addWidget(self.thumbs)
        split.addWidget(self.scroll)
        split.setSizes([160, 940])
        self.setCentralWidget(split)

        for menu_name, items in {
            "&File": [("&Open…", QKeySequence.Open, self.open_dialog), ("&Quit", QKeySequence.Quit, self.close)],
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

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF files (*.pdf)")
        if path:
            self.open(path)

    def open(self, path):
        try:
            doc = pymupdf.open(path)
            prompt = "This PDF is protected. Password:"
            while doc.needs_pass:  # stays True after unlocking, so stop on authenticate()'s result
                pw, ok = QInputDialog.getText(self, "Password", prompt, QLineEdit.Password)
                if not ok:
                    return False
                if doc.authenticate(pw):
                    break
                prompt = "Wrong password. Try again:"
        except Exception as e:
            QMessageBox.critical(self, "Cannot open file", f"{path}\n\n{e}")
            return False
        self.doc = doc
        self.query, self.matches, self.match, self.marks = None, [], -1, {}
        self.find_count.clear()
        self.setWindowTitle(f"{Path(path).name} — PDF Reader")
        self.thumbs.clear()
        blank = QPixmap(self.thumbs.iconSize())
        blank.fill(Qt.white)
        for i in range(len(doc)):
            self.thumbs.addItem(QListWidgetItem(QIcon(blank), str(i + 1)))
        self.build_pages()
        return True

    def build_pages(self):
        for label in self.labels:
            label.deleteLater()
        self.labels = []
        for i, page in enumerate(self.doc):
            label = QLabel(self.pages)
            label.setAccessibleName(f"Page {i + 1}")
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
    main()
