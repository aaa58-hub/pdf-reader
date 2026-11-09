"""PDF -> Word (.docx), written with the stdlib zipfile like the EPUB: no Word library needed.

Same text recovery as the reflowable EPUB (epub.structure): headings, paragraphs and figures;
running heads and page numbers dropped; scanned pages with little text kept as pictures.
"""
import os
import zipfile
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


def convert(pdf_path, docx_path, progress=lambda done, total: True, password=None):
    """Write docx_path from pdf_path. progress(done, total) returning False cancels."""
    doc = epub.open_pdf(pdf_path, password)
    chapters, _ = epub.structure(doc, progress, password, cover=False)
    title = doc.metadata.get("title") or os.path.splitext(os.path.basename(pdf_path))[0]
    size = doc[0].rect if len(doc) else pymupdf.paper_rect("a4")
    epub.write_atomically(docx_path, lambda tmp: write(tmp, chapters, title, doc.metadata.get("author", ""),
                                                       LOCALES[epub.chapters_language(chapters)], size))


def picture(n, cx, cy, alt):
    return ('<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{n}" name="Picture {n}" descr={quoteattr(alt)}/>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
            f'<pic:nvPicPr><pic:cNvPr id="{n}" name="image{n}.jpg"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="img{n}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


def write(path, chapters, title, author, locale, page):
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
                run = picture(len(images), round(w * scale * EMU), round(h * scale * EMU), text)
                props.append('<w:jc w:val="center"/>')
            else:
                run = f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
            body.append(f'<w:p><w:pPr>{"".join(props)}</w:pPr>{run}</w:p>')
    m = round(margin * TWIPS)
    document = (f"{XML}<w:document {NS}><w:body>{''.join(body) or '<w:p/>'}"
                f'<w:sectPr><w:pgSz w:w="{round(page.width * TWIPS)}" w:h="{round(page.height * TWIPS)}"/>'
                f'<w:pgMar w:top="{m}" w:right="{m}" w:bottom="{m}" w:left="{m}" w:header="0" w:footer="0" w:gutter="0"/>'
                "</w:sectPr></w:body></w:document>")
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
