# pdf-reader

A desktop app to read and edit PDFs and convert them to EPUB.

## Download

Get `pdf-reader.exe` from the [Releases](../../releases) page and run it. No install needed.

## Run from source

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python pdf_reader.py [file.pdf]
```

Tests: `.venv\Scripts\pytest`

## License

AGPL-3.0, required by [PyMuPDF](https://pymupdf.readthedocs.io/).
