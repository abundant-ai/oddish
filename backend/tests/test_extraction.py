"""Unit tests for pure document text extraction."""

import os

from oddish.core.extraction import extract_text

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.pdf")


def test_extract_plain_text():
    raw = b"hello world\nsecond line"
    assert (
        extract_text(raw, mime="text/plain", filename="n.txt")
        == "hello world\nsecond line"
    )


def test_extract_markdown_passthrough():
    raw = b"# Title\n\nbody"
    assert extract_text(raw, mime="text/markdown", filename="n.md") == "# Title\n\nbody"


def test_extract_pdf_by_mime():
    with open(_FIXTURE, "rb") as f:
        raw = f.read()
    text = extract_text(raw, mime="application/pdf", filename="sample.pdf")
    assert "expected fixture phrase" in text.lower()


def test_extract_pdf_by_filename_extension():
    with open(_FIXTURE, "rb") as f:
        raw = f.read()
    # No mime hint, but the .pdf extension should route through the PDF path.
    text = extract_text(raw, mime=None, filename="sample.pdf")
    assert "expected fixture phrase" in text.lower()


def test_extract_unknown_mime_falls_back_to_utf8():
    raw = "café".encode("utf-8")
    assert extract_text(raw, mime=None, filename="x") == "café"
