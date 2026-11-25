"""PDF pages -> PNG or JPEG files, one per page, as they look on screen (annotations included)."""
import functools
from pathlib import Path

import epub

FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


def convert(pdf_path, target, first, last, dpi, progress=lambda done, total: True, password=None):
    """Pages first..last (0-based, inclusive) at dpi. One page is saved as `target` itself; more get its
    name plus the page number: book.png -> book-001.png, book-002.png, ... Returns a line for the status bar."""
    target = Path(target)
    if target.suffix.lower() not in FORMATS:
        target = target.with_name(target.name + ".png")
    pages = list(range(first, last + 1))
    digits = len(str(last + 1))
    paths = {i: str(target if len(pages) == 1 else target.with_name(f"{target.stem}-{i + 1:0{digits}d}{target.suffix}"))
             for i in pages}
    # a scanned page at 300 dpi takes a second or more: every CPU core takes a share, like the EPUB page images
    epub.render_pages(pdf_path, password, pages, progress, 0, len(pages),
                      work=functools.partial(_save_chunk, paths=paths, dpi=dpi))
    return paths[first] if len(pages) == 1 else f"{len(pages)} images in {target.parent}"


def _save_chunk(pdf_path, password, indices, paths, dpi):  # runs in a worker process
    doc = epub.open_pdf(pdf_path, password)
    for i in indices:
        pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
        output = FORMATS[Path(paths[i]).suffix.lower()]
        epub.write_atomically(paths[i], lambda tmp: pix.save(tmp, output=output, jpg_quality=90))
    return {i: paths[i] for i in indices}
