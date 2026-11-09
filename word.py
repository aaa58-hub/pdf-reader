"""PDF -> Word (.docx), written with the stdlib zipfile like the EPUB: no Word library needed.

Two modes:
- layout:   every page looks like the PDF. The artwork (pictures, drawings, decorative lettering) is a
            picture behind the page; the text sits on top in frames at its original place, editable.
            Best for picture books, magazines, forms. Scans stay pictures (their hidden OCR text is skipped).
- flow:     same text recovery as the reflowable EPUB (epub.structure): headings, paragraphs and
            figures in one flowing document; running heads and page numbers dropped. Best for prose.
"""
import functools
import itertools
import os
import re
import zipfile
from collections import Counter
from xml.sax.saxutils import escape, quoteattr

import pymupdf

import epub

LOCALES = {"en": "en-US", "it": "it-IT", "fr": "fr-FR", "de": "de-DE", "es": "es-ES"}  # Word wants a region
EMU, TWIPS = 12700, 20  # per point
NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
TINY = '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'  # 1 pt: holds a page's anchors


def convert(pdf_path, docx_path, mode="layout", progress=lambda done, total: True, password=None):
    """Write docx_path from pdf_path. progress(done, total) returning False cancels."""
    doc = epub.open_pdf(pdf_path, password)
    title = doc.metadata.get("title") or os.path.splitext(os.path.basename(pdf_path))[0]
    if mode == "layout":
        body, images, text = layout_body(doc, progress, password)
        language = epub.detect_language(text)
    else:
        chapters, _ = epub.structure(doc, progress, password, cover=False)
        body, images = flow_body(chapters, doc[0].rect if len(doc) else pymupdf.paper_rect("a4"))
        language = epub.chapters_language(chapters)
    epub.write_atomically(docx_path, lambda tmp: write(tmp, body, images, title, doc.metadata.get("author", ""),
                                                       LOCALES[language]))


# ---------------------------------------------------------------- layout

def layout_body(doc, progress, password):
    sizes = Counter()
    for page in doc:
        for s in (s for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
                  for line in b["lines"] for s in line["spans"]):
            sizes[round(s["size"])] += len(s["text"].strip())
    display = 2 * (sizes.most_common(1)[0][0] if sizes else 12)  # bigger type is lettering: Word lacks its font
    # drawing a scanned page takes up to a second: every CPU core takes a share, like the EPUB page images
    pages = epub.render_pages(doc.name, password, list(range(len(doc))), progress, 0, len(doc),
                              work=functools.partial(_layout_chunk, display=display))
    body, images = [], []
    for i in range(len(doc)):
        paras, picture, width, height, _ = pages[i]
        background = ""
        if picture:
            images.append(picture)
            background = graphic(len(images), round(width * EMU), round(height * EMU), f"Page {i + 1}", anchored=True)
        section = (f'<w:sectPr><w:pgSz w:w="{tw(width)}" w:h="{tw(height)}"/>'
                   '<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/>'
                   "</w:sectPr>")
        last = i == len(doc) - 1
        body += paras + [f'<w:p><w:pPr>{TINY}{"" if last else section}</w:pPr>{background}</w:p>']
        if last:
            body.append(section)
    return "".join(body), images, " ".join(pages[i][4] for i in range(min(20, len(doc))))


def _layout_chunk(pdf_path, password, indices, display):  # runs in a worker process
    doc = epub.open_pdf(pdf_path, password)
    return {i: layout_page(doc[i], display) for i in indices}


def layout_page(page, display):
    """(frame paragraphs, background JPEG or None, width, height, some text) for one page."""
    page.remove_rotation()  # our own copy of the file: text and picture now share one coordinate space
    blocks = []
    for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        lines = []
        for line in b["lines"]:
            # visible horizontal body text only: lettering and rotated text stay in the picture,
            # hidden OCR text is skipped (the scan already shows it)
            spans = [s for s in line["spans"] if s["text"].strip() and s["alpha"] and s["size"] < display]
            if spans and abs(line["dir"][1]) < 0.02 and line["dir"][0] > 0:
                lines.append(spans)
        if lines:
            blocks.append(lines)
    if blocks:
        for spans in (s for lines in blocks for s in lines):
            for s in spans:
                page.add_redact_annot(s["bbox"])
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(*[epub.PAGE_IMAGE_HEIGHT / page.rect.height] * 2), alpha=False)
    picture = epub.jpeg(pix) if pix.samples.strip(b"\xff") else None  # an all-text page needs no picture
    text = " ".join("".join(s["text"] for s in spans) for lines in blocks for spans in lines)
    return ([frames(lines, page.rect.width) for lines in blocks], picture, page.rect.width, page.rect.height,
            text[:2000])


def frames(lines, page_width):
    """One block of text as a frame at its place on the page; lines keep their breaks."""
    x0 = [s[0]["bbox"][0] for s in lines]
    x1 = [s[-1]["bbox"][2] for s in lines]
    base = [s[0]["origin"][1] for s in lines]
    gaps = [b - a for a, b in zip(base, base[1:])]
    size = max(s["size"] for s in lines[0])

    def same(values):
        return not values or max(values) - min(values) < 2

    if len(lines) > 1 and not (same(gaps) and gaps[0] > 0):
        return "".join(frames([line], page_width) for line in lines)  # irregular: one frame per line
    pitch = gaps[0] if gaps else size * 1.2
    if same(x0[1:]):  # left aligned, maybe with an indented or hanging first line
        rest = x0[1] if len(x0) > 1 else x0[0]
        x = min(x0[0], rest)
        first = x0[0] - rest
        indent = (f'<w:ind w:left="{tw(rest - x)}" ' +
                  (f'w:firstLine="{tw(first)}"/>' if first >= 0 else f'w:hanging="{tw(-first)}"/>'))
        jc, width = "left", page_width - x  # room to spare: a wider substitute font mustn't re-wrap lines
    elif same([(a + b) / 2 for a, b in zip(x0, x1)]):
        middle = (x0[0] + x1[0]) / 2
        half = min(middle, page_width - middle)
        indent, jc, x, width = "", "center", middle - half, 2 * half
    elif same(x1):
        indent, jc, x, width = "", "right", 0, x1[0]
    else:
        return "".join(frames([line], page_width) for line in lines)
    runs = '<w:r><w:br/></w:r>'.join("".join(run(s) for s in spans) for spans in lines)
    y = max(0, base[0] - pitch + 0.22 * size)  # Word sets an exact-height line's baseline about a descent above its bottom
    return (f'<w:p><w:pPr><w:framePr w:w="{tw(width)}" w:x="{tw(x)}" w:y="{tw(y)}" w:hAnchor="page" '
            f'w:vAnchor="page" w:wrap="around"/><w:spacing w:before="0" w:after="0" w:line="{tw(pitch)}" '
            f'w:lineRule="exact"/>{indent}<w:jc w:val="{jc}"/></w:pPr>{runs}</w:p>')


def run(span):
    text = epub.CONTROL.sub("", span["text"])
    bold = bool(span["flags"] & 16 or re.search("bold|black|heavy", span["font"], re.I))
    italic = bool(span["flags"] & 2 or re.search("italic|oblique", span["font"], re.I))
    font, scale = font_family(span["font"]), ""
    if font not in installed_fonts():
        # Word would pick some font of its own, wider or narrower: pick one whose letter widths we know
        # and stretch the text to the width it has in the PDF, so lines don't run into the pictures
        kind = 2 if span["flags"] & 8 else 0 if span["flags"] & 4 else 1
        font = SUBSTITUTES[kind]
        natural = base14(BASE14[kind][bold * 2 + italic]).text_length(text, span["size"])
        percent = round(100 * (span["bbox"][2] - span["bbox"][0]) / natural) if natural else 100
        if abs(percent - 100) > 2:
            scale = f'<w:w w:val="{min(max(percent, 33), 300)}"/>'
    props = (f'<w:rFonts w:ascii={quoteattr(font)} w:hAnsi={quoteattr(font)} w:cs={quoteattr(font)}/>'
             + "<w:b/>" * bold + "<w:i/>" * italic + f'<w:color w:val="{span["color"]:06X}"/>{scale}'
             f'<w:sz w:val="{round(span["size"] * 2)}"/>')
    return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


SUBSTITUTES = ("Times New Roman", "Arial", "Courier New")  # metric twins of the PDF base fonts below
BASE14 = (("tiro", "tiit", "tibo", "tibi"), ("helv", "heit", "hebo", "hebi"), ("cour", "coit", "cobo", "cobi"))


@functools.cache
def base14(name):
    return pymupdf.Font(name)


@functools.cache
def installed_fonts():
    """Font families installed on this Windows machine (none elsewhere: then everything is substituted)."""
    try:
        import winreg
    except ImportError:
        return frozenset()
    names = set()
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        with key:
            for i in itertools.count():
                try:
                    value = winreg.EnumValue(key, i)[0]  # "Cambria & Cambria Math (TrueType)"
                except OSError:
                    break
                names.update(n.strip() for n in re.sub(r"\(.*\)", "", value).split("&"))
    return frozenset(names)


def font_family(name):
    """'ABCDEF+TimesNewRomanPS-BoldMT' -> 'Times New Roman': the name Word knows the font by."""
    name = re.split("[-,]", name.split("+")[-1])[0]
    name = re.sub("(PS)?MT$|PS$", "", name)
    return re.sub("(?<=[a-z])(?=[A-Z])", " ", name)


def tw(points):
    return round(points * TWIPS)


# ---------------------------------------------------------------- flow

def flow_body(chapters, page):
    margin = min(72, page.width * 0.12)  # 1 inch, less on small book pages
    box_w, box_h = page.width - 2 * margin, page.height - 2 * margin
    body, images = [], []
    for c, (chapter_title, parts, _) in enumerate(chapters):
        for k, (kind, text, image) in enumerate(parts):
            props = ['<w:pStyle w:val="Heading1"/>'] if kind == "h" else []
            if c and not k and chapter_title:
                props.append("<w:pageBreakBefore/>")  # chapters start on a new page, like the book
            if kind == "img":
                data, w, h = image
                scale = min(1, box_w / w, box_h / h)  # natural size, shrunk to fit the page
                images.append(data)
                run_xml = graphic(len(images), round(w * scale * EMU), round(h * scale * EMU), text)
                props.append('<w:jc w:val="center"/>')
            else:
                run_xml = f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
            body.append(f'<w:p><w:pPr>{"".join(props)}</w:pPr>{run_xml}</w:p>')
    m = tw(margin)
    body.append(f'<w:sectPr><w:pgSz w:w="{tw(page.width)}" w:h="{tw(page.height)}"/>'
                f'<w:pgMar w:top="{m}" w:right="{m}" w:bottom="{m}" w:left="{m}" w:header="0" w:footer="0" w:gutter="0"/>'
                "</w:sectPr>")
    return "".join(body) if len(body) > 1 else "<w:p/>" + body[0], images


# ---------------------------------------------------------------- package

def graphic(n, cx, cy, alt, anchored=False):
    """Picture n: inline in the text, or anchored behind the page at its top-left corner."""
    pic = (f'<wp:extent cx="{cx}" cy="{cy}"/>' + ('<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>' if anchored else "") +
           f'<wp:docPr id="{n}" name="Picture {n}" descr={quoteattr(alt)}/>'
           '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
           f'<pic:nvPicPr><pic:cNvPr id="{n}" name="image{n}.jpg"/><pic:cNvPicPr/></pic:nvPicPr>'
           f'<pic:blipFill><a:blip r:embed="img{n}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
           f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
           '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
           '</pic:pic></a:graphicData></a:graphic>')
    if anchored:
        return ('<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" '
                'behindDoc="1" locked="1" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
                '<wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>'
                '<wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>'
                f'{pic}</wp:anchor></w:drawing></w:r>')
    return f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">{pic}</wp:inline></w:drawing></w:r>'


def write(path, body, images, title, author, locale):
    document = f"{XML}<w:document {NS}><w:body>{body}</w:body></w:document>"
    styles = (f'{XML}<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              f'<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="22"/><w:lang w:val="{locale}"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
              '</w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
              '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="480" w:after="240"/>'
              '<w:jc w:val="center"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
              "</w:styles>")
    rels = "".join(f'<Relationship Id="img{n}" Type="{REL}/image" Target="media/image{n}.jpg"/>'
                   for n in range(1, len(images) + 1))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="jpg" ContentType="image/jpeg"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels", f'{XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   "</Relationships>")
        z.writestr("docProps/core.xml", f'{XML}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>"
                   f"<dc:language>{locale}</dc:language></cp:coreProperties>")
        z.writestr("word/_rels/document.xml.rels", f'{XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="styles" Type="{REL}/styles" Target="styles.xml"/>{rels}</Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        for n, data in enumerate(images, 1):
            z.writestr(f"word/media/image{n}.jpg", data, compress_type=zipfile.ZIP_STORED)  # already compressed
