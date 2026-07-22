"""ASCII-normalize smart typography in task metadata."""

from __future__ import annotations

import unicodedata

_REPLACEMENTS: dict[str, str] = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "--",
    "―": "--",
    "−": "-",
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "′": "'",
    "″": '"',
    "…": "...",
    "•": "*",
    **{
        chr(cp): " "
        for cp in (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)
    },
    **{chr(cp): "" for cp in (0x200B, 0x00AD, 0xFEFF)},
}


def _normalize_char(ch: str) -> str | None:
    if ch in _REPLACEMENTS:
        return _REPLACEMENTS[ch]
    if ch.isascii() or not 0x00C0 <= ord(ch) < 0x0250:
        return None
    base = "".join(
        c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
    )
    return base if base.isascii() and base.isalpha() else None


def normalize_typography(text: str) -> str:
    return "".join(
        ch if (replacement := _normalize_char(ch)) is None else replacement
        for ch in text
    )


def summarize_normalization(text: str) -> dict[str, str]:
    return {
        ch: replacement
        for ch in text
        if (replacement := _normalize_char(ch)) is not None
    }
