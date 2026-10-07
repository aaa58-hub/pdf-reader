import sys
from pathlib import Path

import pymupdf
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QScrollArea, QSplitter, QWidget,
)

ZOOM_STEP, ZOOM_MIN, ZOOM_MAX = 1.25, 0.25, 5.0
THUMB_ZOOM = 0.2
GAP = 8  # px around pages


def render(page, zoom, dpr=1.0):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom * dpr, zoom * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()  # copy: samples die with pix
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)  # sharp text on 125%/150% Windows scaling
    return pm


class Viewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Reader")
        self.resize(1100, 800)
        self.doc, self.zoom, self.labels = None, 1.0, []

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

        split = QSplitter()
        split.addWidget(self.thumbs)
        split.addWidget(self.scroll)
        split.setSizes([160, 940])
        self.setCentralWidget(split)

        for menu_name, items in {
            "&File": [("&Open…", QKeySequence.Open, self.open_dialog), ("&Quit", QKeySequence.Quit, self.close)],
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
            self.labels[i].setPixmap(render(self.doc[i], self.zoom, dpr))
            self.labels[i].rendered = True
        else:
            self.thumbs.item(i).setIcon(QIcon(render(self.doc[i], THUMB_ZOOM, dpr)))
            self.thumbs.item(i).setData(Qt.UserRole, True)  # thumbnails are small: keep them once drawn
        QTimer.singleShot(1, self.render_visible)  # 1ms, not 0: lets timers and input run in between

    def follow_in_sidebar(self):
        """Highlight the current page's thumbnail and scroll it into view, without jumping the document."""
        page = self.current_page()
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
