"""A page range of a PDF saved as a new PDF: text, pictures, links, annotations and bookmarks kept."""
import os

import pymupdf

import epub


def convert(pdf_path, target, first, last, progress=lambda done, total: True, password=None):
    """Pages first..last (0-based, inclusive) of pdf_path into target. A protected PDF stays protected,
    with the password it was opened with."""
    if os.path.exists(target) and os.path.samefile(target, pdf_path):
        raise ValueError("That's the PDF you are reading. Choose another name.")
    if not progress(0, 1):
        raise epub.Cancelled
    src = epub.open_pdf(pdf_path, password)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=first, to_page=last)  # brings the pages' links and annotations along
    out.set_metadata({k: src.metadata.get(k) or "" for k in ("title", "author", "subject", "keywords")})
    toc, level = [], 0
    for depth, title, page in src.get_toc():  # the bookmarks that point into the range
        if first < page <= last + 1:
            level = min(depth, level + 1)  # a sub-bookmark whose parent fell outside moves up
            toc.append([level, title, page - first])
    out.set_toc(toc)
    protect = dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password) if password else {}
    epub.write_atomically(target, lambda tmp: out.save(tmp, garbage=3, deflate=True, **protect))
    progress(1, 1)
