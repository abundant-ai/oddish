"""Pure text extraction for ingested documents.

No DB/S3/network — takes raw bytes + a content-type hint and returns plain
text suitable for digesting and (future) search. PDFs go through ``pypdf``;
everything else is decoded as UTF-8 (text/markdown/unknown).
"""

from __future__ import annotations

import io


def extract_text(raw: bytes, *, mime: str | None, filename: str | None) -> str:
    """Return plain text extracted from ``raw``.

    PDF (by mime or ``.pdf`` filename) is parsed page-by-page; all other
    inputs are decoded UTF-8 with replacement so ingest never hard-fails on
    odd bytes.
    """
    is_pdf = (mime == "application/pdf") or (
        filename is not None and filename.lower().endswith(".pdf")
    )
    if is_pdf:
        return _extract_pdf(raw)
    return raw.decode("utf-8", errors="replace")


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(parts).strip()
