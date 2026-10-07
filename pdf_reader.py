import sys
from pathlib import Path

import pymupdf
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

ZOOM_STEP, ZOOM_MIN, ZOOM_MAX = 1.25, 0.25, 5.0


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

        self.pages = QWidget()
        self.pages.setStyleSheet("background: #c8c8c8")  # page edges stand out
        self.column = QVBoxLayout(self.pages)
        self.column.setAlignment(Qt.AlignHCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.pages)
        self.scroll.setAccessibleName("Document")
        self.scroll.verticalScrollBar().valueChanged.connect(self.render_visible)

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
        # ponytail: all thumbnails rendered up front; make lazy if 1000+ page files feel slow to open
        for i, page in enumerate(doc):
            self.thumbs.addItem(QListWidgetItem(render(page, 0.2, self.devicePixelRatioF()), str(i + 1)))
        self.build_pages()
        return True

    def build_pages(self):
        for label in self.labels:
            label.deleteLater()
        self.labels = []
        for i, page in enumerate(self.doc):
            label = QLabel()
            label.setAccessibleName(f"Page {i + 1}")
            label.setFixedSize(int(page.rect.width * self.zoom), int(page.rect.height * self.zoom))
            label.setStyleSheet("background: white")
            label.rendered = False
            self.column.addWidget(label)
            label.show()  # Qt otherwise shows it on the next tick, and the layout skips hidden widgets
            self.labels.append(label)
        QTimer.singleShot(0, self.render_visible)  # geometry is only known after layout

    def set_zoom(self, zoom):
        if not self.doc:
            return
        top_page = self.current_page()
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        for label, page in zip(self.labels, self.doc):
            label.setFixedSize(int(page.rect.width * self.zoom), int(page.rect.height * self.zoom))
            label.clear()
            label.rendered = False
        QTimer.singleShot(0, lambda: self.go_to(top_page))

    def current_page(self):
        y = self.scroll.verticalScrollBar().value()
        return next((i for i, l in enumerate(self.labels) if l.geometry().bottom() >= y), 0)

    def go_to(self, index):
        if 0 <= index < len(self.labels):
            self.sync_layout()
            self.scroll.verticalScrollBar().setValue(self.labels[index].y())
            self.render_visible()

    def render_visible(self):
        """Render only pages in view and free the rest, so memory stays flat on big files."""
        self.sync_layout()
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        for label, page in zip(self.labels, self.doc or []):
            visible = label.y() <= bottom and label.y() + label.height() >= top
            if visible and not label.rendered:
                label.setPixmap(render(page, self.zoom, self.devicePixelRatioF()))
                label.rendered = True
            elif not visible and label.rendered:
                label.clear()
                label.rendered = False

    def sync_layout(self):
        """Place pages now instead of on the next event-loop tick, so y positions and scroll range are real."""
        self.column.activate()
        self.pages.resize(self.pages.width(), self.column.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render_visible()


def main():
    app = QApplication(sys.argv)
    viewer = Viewer()
    viewer.show()
    if len(sys.argv) > 1:
        viewer.open(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
