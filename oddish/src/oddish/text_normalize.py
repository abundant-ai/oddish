"""ASCII-normalize the "smart" typography LLM-authored task text carries.

Task metadata written by a model routinely picks up curly quotes, en/em
dashes, ellipses, and no-break spaces. They are perfectly valid UTF-8, but in
a monospace file viewer they render as unexpected glyphs (and add nothing over
their ASCII equivalents), so a reviewer sees "a bad character somewhere" in an
otherwise clean ``task.toml``.

The normalizer rewrites that punctuation to ASCII and strips diacritics off
Latin letters (``Hölder`` -> ``Holder``). It deliberately leaves every other
script alone -- Greek, Cyrillic, CJK, letterlike math symbols (``ℝ``), relation
and arrow glyphs (``≤``, ``→``), and emoji -- because a task may legitimately
reference those, and mangling them would corrupt real content.
"""

from __future__ import annotations

import unicodedata

# Dashes/hyphens, quotes, and other punctuation a model substitutes for the
# plain ASCII form. Keyed by the offending codepoint; the visible glyph is
# named in the trailing comment so the mapping is reviewable.
_PUNCTUATION_MAP: dict[str, str] = {
    "‐": "-",  # ‐ hyphen
    "‑": "-",  # ‑ non-breaking hyphen
    "‒": "-",  # ‒ figure dash
    "–": "-",  # – en dash
    "—": "--",  # — em dash
    "―": "--",  # ― horizontal bar
    "−": "-",  # − minus sign
    "‘": "'",  # ‘ left single quotation mark
    "’": "'",  # ’ right single quotation mark / apostrophe
    "‚": "'",  # ‚ single low-9 quotation mark
    "‛": "'",  # ‛ single high-reversed-9 quotation mark
    "“": '"',  # “ left double quotation mark
    "”": '"',  # ” right double quotation mark
    "„": '"',  # „ double low-9 quotation mark
    "‟": '"',  # ‟ double high-reversed-9 quotation mark
    "′": "'",  # ′ prime
    "″": '"',  # ″ double prime
    "…": "...",  # … horizontal ellipsis
    "•": "*",  # • bullet
}

# Whitespace variants -> a plain space. Built from codepoints so no invisible
# glyphs sit in the source (they are unreviewable and editors mangle them):
#   00A0 no-break, 1680 ogham, 2000-200A en/em/thin/hair family,
#   202F narrow no-break, 205F medium math, 3000 ideographic.
_SPACE_MAP: dict[str, str] = {
    chr(cp): " "
    for cp in (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)
}

# Zero-width and byte-order marks -> removed entirely:
#   200B zero-width space, 200C non-joiner, 200D joiner, 2060 word joiner,
#   FEFF zero-width no-break / BOM.
_ZERO_WIDTH: frozenset[str] = frozenset(
    chr(cp) for cp in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
)

# Accented Latin letters live in these blocks (Latin-1 Supplement plus Latin
# Extended-A/B). Gating on the range is what keeps us from decomposing
# letterlike math symbols such as ``ℝ`` (U+211D, NFKD -> "R"), Greek, or
# Cyrillic, which fall outside it.
_LATIN_ACCENT_RANGE = range(0x00C0, 0x0250)


def _transliterate_latin(ch: str) -> str:
    """Strip the diacritic from a Latin letter (``ö`` -> ``o``); else unchanged."""
    if ord(ch) not in _LATIN_ACCENT_RANGE:
        return ch
    base = "".join(
        c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
    )
    if base.isascii() and base.isalpha():
        return base
    return ch


def _normalize_char(ch: str) -> str | None:
    """Return the ASCII rewrite for one character, or ``None`` to keep it as-is.

    An empty-string return means "drop this character" (zero-width marks).
    """
    if ch in _ZERO_WIDTH:
        return ""
    if ch in _PUNCTUATION_MAP:
        return _PUNCTUATION_MAP[ch]
    if ch in _SPACE_MAP:
        return _SPACE_MAP[ch]
    if ch.isascii():  # covers plain text, "\n", and "\t"
        return None
    transliterated = _transliterate_latin(ch)
    return transliterated if transliterated != ch else None


def normalize_typography(text: str) -> str:
    """Rewrite smart typography as plain ASCII, leaving real content untouched."""
    out: list[str] = []
    for ch in text:
        replacement = _normalize_char(ch)
        out.append(ch if replacement is None else replacement)
    return "".join(out)


def summarize_normalization(text: str) -> dict[str, str]:
    """Map each distinct character ``normalize_typography`` would rewrite to its
    replacement. Empty when the text is already clean. Intended for logging."""
    changes: dict[str, str] = {}
    for ch in text:
        replacement = _normalize_char(ch)
        if replacement is not None:
            changes[ch] = replacement
    return changes
