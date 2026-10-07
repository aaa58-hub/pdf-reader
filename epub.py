"""PDF -> EPUB 3, written with the stdlib zipfile: no EPUB library needed.

Two modes:
- reflow:   text that adapts to the reader's screen. Headings, paragraphs and pictures are
            recovered heuristically; running heads and page numbers are dropped.
- fixed:    every page as a picture (fixed-layout EPUB). Looks exactly like the PDF, best for scans.
"""
import html
import os
import re
import uuid
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

import pymupdf

PAGE_IMAGE_HEIGHT = 1200  # px: sharp on phones and e-readers; scans compress badly, so not more
SCAN_COVERAGE = 0.7  # an image covering this much of a page is a scan background, not a figure
MARGIN = 0.1  # top/bottom share of the page where running heads and page numbers live
STOPWORDS = {
    "en": "the and of to in is that it was for".split(),
    "it": "il di che la per un non una sono della".split(),
    "fr": "le de la et les des est une que pour".split(),
    "de": "der die und das ist nicht mit den ein sich".split(),
    "es": "el la de que y los las por una con".split(),
}
# characters ordinary prose doesn't use: lots of them means OCR read a picture as text
ODD_CHARS = re.compile(r"[^\w\s.,;:!?'\"()\[\]\-\u2013\u2014\u2018\u2019\u201c\u201d&%/]")
ROMAN = re.compile(r"[IVXLC]+\.?")
CSS = """body { font-family: serif; line-height: 1.5; margin: 0 5%; }
h1, h2 { text-align: center; line-height: 1.2; margin: 2em 0 1em; }
p { text-indent: 1.5em; margin: 0; text-align: justify; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
img { max-width: 100%; max-height: 95vh; }
"""


class Cancelled(Exception):
    pass


def convert(pdf_path, epub_path, mode="reflow", progress=lambda done, total: True, password=None):
    """Write epub_path from pdf_path. progress(done, total) returning False cancels."""
    doc = open_pdf(pdf_path, password)
    title = doc.metadata.get("title") or os.path.splitext(os.path.basename(pdf_path))[0]
    book = Book(title, doc.metadata.get("author", ""))
    if mode == "fixed":
        fixed_layout(doc, book, progress, password)
    else:
        reflow(doc, book, progress, password)
    tmp = epub_path + ".part"  # never leave a half-written file under the real name
    try:
        book.write(tmp)
        os.replace(tmp, epub_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def jpeg(pix):
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)  # JPEG has no transparency
    if pix.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # CMYK and friends aren't valid in EPUB JPEGs
    return pix.tobytes("jpeg", jpg_quality=70)


def page_jpeg(page):
    zoom = PAGE_IMAGE_HEIGHT / page.rect.height
    return jpeg(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False))


def open_pdf(path, password):
    doc = pymupdf.open(path)
    if password:
        doc.authenticate(password)
    return doc


def _render_chunk(pdf_path, password, indices):  # runs in a worker process
    doc = open_pdf(pdf_path, password)
    return {i: page_jpeg(doc[i]) for i in indices}


def render_pages(pdf_path, password, indices, progress, done, total):
    """{index: JPEG} for the given pages, drawn on every CPU core: a scanned page costs ~0.3s each."""
    pages = {}
    chunks = [indices[k:k + 2] for k in range(0, len(indices), 2)]  # small chunks: progress bar stays live
    with ProcessPoolExecutor() as pool:
        futures = [pool.submit(_render_chunk, pdf_path, password, chunk) for chunk in chunks]
        for future in as_completed(futures):
            pages.update(future.result())
            if not progress(done + len(pages), total):
                pool.shutdown(cancel_futures=True)
                raise Cancelled
    return pages


def toc_starts(doc):
    """{page index: title} from the PDF's own bookmarks (top two levels), if it has any."""
    starts = {}
    for level, title, page in doc.get_toc():
        if level <= 2 and page >= 1:
            starts.setdefault(page - 1, title.strip())
    return starts


# ---------------------------------------------------------------- fixed layout

def fixed_layout(doc, book, progress, password):
    book.fixed = True
    bookmarks = toc_starts(doc)
    images = render_pages(doc.name, password, list(range(len(doc))), progress, 0, len(doc))
    for i, page in enumerate(doc):
        img = book.add_image(images[i], "jpg", cover=i == 0)
        w, h = round(page.rect.width), round(page.rect.height)
        body = f'<img src="{img}" alt="Page {i + 1}" style="width:100%;height:100%"/>'
        # ponytail: TOC = bookmarks, else one entry per 10 pages; per-page entries if readers want them
        label = bookmarks.get(i) or (f"Page {i + 1}" if not bookmarks and i % 10 == 0 else None)
        book.add_chapter(label, body, viewport=(w, h), title=f"Page {i + 1}")


# ---------------------------------------------------------------- reflow

def block_text(block):
    """Join a text block's lines into one string, undoing end-of-line hyphenation."""
    text = ""
    for line in block["lines"]:
        part = re.sub(r"\s+", " ", "".join(s["text"] for s in line["spans"])).strip()
        if not part:
            continue
        if re.search(r"\w[-�]$", text) and part[:1].islower():
            text = text[:-1] + part  # "manu-" + "facture"
        else:
            text = f"{text} {part}" if text else part
    text = re.sub(r"(?<=\w)�(?=\w)", "’", text)  # OCR loses curly apostrophes as U+FFFD
    return text.replace("�", "").strip()


def block_size(block):
    """Font size covering most of the block's characters (OCR spans have wild outliers)."""
    sizes = Counter()
    for line in block["lines"]:
        for s in line["spans"]:
            sizes[round(s["size"] * 2) / 2] += len(s["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 0


def is_noise(text):
    return len(ODD_CHARS.findall(text)) > 0.05 * len(text)


def looks_like_heading(text):
    """Short and capitalised: 'BINDING', 'The Printer's Reader', 'IV'. Not a big-type sample sentence."""
    words = re.findall(r"[^\W\d_]+", text)
    letters = sum(len(w) for w in words)
    if ROMAN.fullmatch(text):
        return True
    return (letters >= 3 and letters >= 0.6 * len(text.replace(" ", ""))
            and sum(w[0].isupper() for w in words) >= 0.6 * len(words))


def reflow(doc, book, progress, password):
    n = len(doc)
    pages = []  # per page: list of (kind, payload) in reading order
    heads = Counter()  # normalised top-margin text -> pages it appears on
    sizes = Counter()
    for i, page in enumerate(doc):
        if not progress(i, n):
            raise Cancelled
        area = page.rect.width * page.rect.height
        blocks, scanned, figures = [], False, []
        # positions only: get_text(images) / get_image_info(xrefs) decode every scan, ~0.2s a page
        for img in page.get_images(full=True):
            xref, width, height = img[0], img[2], img[3]
            bbox = page.get_image_bbox(img)
            if bbox.is_infinite or bbox.is_empty:
                continue  # listed in resources but not drawn
            if bbox.width * bbox.height > SCAN_COVERAGE * area:
                scanned = True
            elif width >= 32 and height >= 32:
                figures.append((bbox.y0, xref))
        figures.sort()
        no_images = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        for b in page.get_text("dict", flags=no_images)["blocks"]:
            x0, y0, x1, y1 = b["bbox"]
            while figures and figures[0][0] <= y0:  # figure goes before the first text below it
                blocks.append(("img", figures.pop(0)[1]))
            text = block_text(b)
            if not text or is_noise(text):
                continue
            size = block_size(b)
            top, bottom = y1 < page.rect.height * MARGIN, y0 > page.rect.height * (1 - MARGIN)
            if (top or bottom) and len(text) <= 12:
                continue  # page number, printer's signature mark
            key = re.sub(r"[^a-z]", "", text.lower())
            if top:
                heads[key] += 1
            sizes[size] += len(text)
            blocks.append(("text", (text, size, len(b["lines"]), key if top else None)))
        blocks += [("img", xref) for _, xref in figures]
        words = sum(len(p[0].split()) for kind, p in blocks if kind == "text")
        if scanned and words < 40:  # plate, title page, illustration: keep the page as a picture
            blocks = [("page", i)]
        pages.append(blocks)

    pictured = sorted({0} | {i for i, blocks in enumerate(pages) if blocks[:1] == [("page", i)]})
    pictures = render_pages(doc.name, password, pictured, progress, n, n + len(pictured))

    body = sizes.most_common(1)[0][0] if sizes else 10
    bookmarks = toc_starts(doc)
    chapters = []  # [title, [html parts], first page]

    def start_chapter(title, page_index):
        if chapters and not chapters[-1][1]:
            chapters[-1][0] = chapters[-1][0] or title  # nothing written yet: just retitle it
        else:
            chapters.append([title, [], page_index])

    last_para = None  # [text, page index] of the paragraph that may continue on the next page
    for i, blocks in enumerate(pages):
        if i in bookmarks or not chapters or (not bookmarks and i - chapters[-1][2] >= 40):
            start_chapter(bookmarks.get(i), i)  # 40-page cap: huge files are slow on e-readers
        for kind, payload in blocks:
            parts = chapters[-1][1]
            if kind in ("img", "page"):
                data = pictures[payload] if kind == "page" else jpeg(pymupdf.Pixmap(doc, payload))
                name = book.add_image(data, "jpg")
                alt = f"Page {i + 1}" if kind == "page" else f"Figure on page {i + 1}"
                parts.append(f'<figure><img src="{name}" alt="{alt}"/></figure>')
                last_para = None
                continue
            text, size, lines, head_key = payload
            if head_key and heads[head_key] >= 3:
                continue  # running head repeated across pages
            if size >= body * 1.3 and lines <= 3 and len(text) <= 100 and looks_like_heading(text):
                only_headings = all(part.startswith("<h2>") for part in parts)
                if not bookmarks and not only_headings:
                    start_chapter(text, i)
                    parts = chapters[-1][1]
                elif not bookmarks and parts and len(chapters[-1][0] or "") <= 12:
                    # "IV" then "COMPOSING MACHINES": one chapter, one title
                    chapters[-1][0] = f"{chapters[-1][0]} \u2014 {text}" if chapters[-1][0] else text
                elif not chapters[-1][0]:
                    chapters[-1][0] = text
                parts.append(f"<h2>{html.escape(text)}</h2>")
                last_para = None
                continue
            if (last_para and last_para[1] >= i - 1 and parts and parts[-1].startswith("<p>")
                    and not re.search(r"[.!?:;\"”’)]$", last_para[0]) and text[:1].islower()):
                # sentence split across blocks (one block per line) or by the page turn: glue it back
                joined = last_para[0][:-1] + text if last_para[0].endswith("-") else f"{last_para[0]} {text}"
                parts[-1] = f"<p>{html.escape(joined)}</p>"
                last_para = [joined, i]
            else:
                parts.append(f"<p>{html.escape(text)}</p>")
                last_para = [text, i]

    book.language = detect_language(" ".join(re.sub("<[^>]+>", " ", "".join(c[1])) for c in chapters[:20]))
    if n:
        book.add_image(pictures[0], "jpg", cover=True)
    for title, parts, first in chapters:
        if parts:
            book.add_chapter(title or f"From page {first + 1}", "\n".join(parts))


def detect_language(text):
    words = Counter(re.findall(r"[^\W\d_]+", text.lower()))
    return max(STOPWORDS, key=lambda lang: sum(words[w] for w in STOPWORDS[lang]))


# ---------------------------------------------------------------- EPUB container

class Book:
    def __init__(self, title, author):
        self.title, self.author, self.language = title, author, "en"
        self.fixed = False
        self.chapters = []  # (file name, toc label or None, xhtml)
        self.images = []  # (file name, bytes, is cover)

    def add_image(self, data, ext, cover=False):
        name = f"images/img{len(self.images) + 1:04}.{ext}"
        self.images.append((name, data, cover))
        return name

    def add_chapter(self, label, body, viewport=None, title=None):
        name = f"text/part{len(self.chapters) + 1:04}.xhtml"
        if viewport:  # fixed layout: the page picture fills the screen
            head = f'<meta name="viewport" content="width={viewport[0]}, height={viewport[1]}"/>'
            body_tag = '<body style="margin:0">'
        else:
            head = '<link rel="stylesheet" type="text/css" href="../style.css"/>'
            body_tag = "<body>"
        body = body.replace('src="images/', 'src="../images/')
        xhtml = (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                 f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{{lang}}" lang="{{lang}}">\n'
                 f"<head><title>{html.escape(title or label or self.title)}</title>{head}</head>\n"
                 f"{body_tag}\n{body}\n</body></html>")
        self.chapters.append((name, label, xhtml))

    def write(self, path):
        lang = html.escape(self.language)
        esc = html.escape
        toc = "\n".join(f'<li><a href="{name}">{esc(label)}</a></li>' for name, label, _ in self.chapters if label)
        nav = (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
               f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
               f'xml:lang="{lang}" lang="{lang}">\n<head><title>{esc(self.title)}</title></head>\n'
               f'<body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n{toc}\n</ol></nav></body></html>')
        manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                    '<item id="css" href="style.css" media-type="text/css"/>']
        manifest += [f'<item id="c{k}" href="{name}" media-type="application/xhtml+xml"/>'
                     for k, (name, _, _) in enumerate(self.chapters)]
        manifest += [f'<item id="i{k}" href="{name}" media-type="image/jpeg"{" properties=\"cover-image\"" if cover else ""}/>'
                     for k, (name, _, cover) in enumerate(self.images)]
        spine = "\n".join(f'<itemref idref="c{k}"/>' for k in range(len(self.chapters)))
        layout = ('<meta property="rendition:layout">pre-paginated</meta>\n'
                  '<meta property="rendition:spread">none</meta>\n') if self.fixed else ""
        author = f"<dc:creator>{esc(self.author)}</dc:creator>\n" if self.author else ""
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = (f'<?xml version="1.0" encoding="utf-8"?>\n'
               f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="{lang}">\n'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
               f'<dc:identifier id="uid">{uuid.uuid4().urn}</dc:identifier>\n'
               f"<dc:title>{esc(self.title)}</dc:title>\n<dc:language>{lang}</dc:language>\n{author}"
               f'<meta property="dcterms:modified">{modified}</meta>\n{layout}</metadata>\n'
               f"<manifest>\n" + "\n".join(manifest) + f"\n</manifest>\n<spine>\n{spine}\n</spine>\n</package>")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)  # must be first, uncompressed
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0" encoding="utf-8"?>\n'
                       '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                       "</rootfiles></container>")
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/nav.xhtml", nav)
            z.writestr("OEBPS/style.css", CSS)
            for name, _, xhtml in self.chapters:
                z.writestr(f"OEBPS/{name}", xhtml.replace("{lang}", lang))
            for name, data, _ in self.images:
                z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_STORED)  # JPEG is already compressed
