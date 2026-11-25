"""Every export that turns pages into pictures keeps highlights and leaves note icons out."""
import zipfile

import pymupdf
import pytest

import epub
import images
import word

NOTE_AT = pymupdf.Point(200, 200)


@pytest.fixture
def annotated(tmp_path):
    path = tmp_path / "annotated.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((20, 50), "Highlighted words", fontsize=14)
    page.add_highlight_annot(page.search_for("Highlighted"))
    page.add_text_annot(NOTE_AT, "A note")
    doc.save(path)
    return path


def jpegs(path):
    z = zipfile.ZipFile(path)
    return [z.read(n) for n in z.namelist() if n.endswith(".jpg")]


def epub_page_images(pdf, tmp_path):
    out = tmp_path / "pages.epub"
    epub.convert(str(pdf), str(out), "fixed")
    return jpegs(out)


def word_page_layout(pdf, tmp_path):
    out = tmp_path / "layout.docx"
    word.convert(str(pdf), str(out), "layout")
    return jpegs(out)


def png_image(pdf, tmp_path):
    out = tmp_path / "page.png"
    images.convert(str(pdf), str(out), 0, 0, 150)
    return [out.read_bytes()]


@pytest.mark.parametrize("export", [epub_page_images, word_page_layout, png_image])
def test_highlight_kept_note_icon_left_out(annotated, tmp_path, export):
    pictures = export(annotated, tmp_path)
    assert pictures
    for data in pictures:
        pix = pymupdf.Pixmap(data)
        scale = pix.height / 400

        def colour(x, y):
            return pix.pixel(int(x * scale), int(y * scale))

        assert colour(25, 46)[2] < 150, "highlight painted (yellow: little blue)"
        assert all(min(colour(NOTE_AT.x + dx, NOTE_AT.y + dy)) > 225 for dx in range(2, 18, 3) for dy in range(2, 18, 3)), \
            "no note icon, just the white page"
    assert {a.type[1] for a in pymupdf.open(annotated)[0].annots()} == {"Highlight", "Text"}, "the PDF keeps its note"
