# pdf-reader

Python + PySide6 (UI) + PyMuPDF (PDF engine). Windows-first desktop app, released as a single `.exe` via GitHub Actions on `v*` tags.

## Rules

1. One feature per PR, branch off `main`.
2. Every new UI behaviour gets a pytest-qt test in `tests/`. Tests run offscreen and must pass in CI.
3. Keep it minimal: no new dependency when PyMuPDF, Qt or the stdlib already cover it.
4. The app opens untrusted files: never execute embedded PDF JavaScript or launch embedded attachments.
5. Commit messages: no `Co-Authored-By` trailer.

## Commands

- Run: `.venv\Scripts\python pdf_reader.py [file.pdf]`
- Test: `.venv\Scripts\pytest`
- Release: push a tag `vX.Y.Z` — CI builds `pdf-reader.exe` and attaches it to a GitHub Release.
