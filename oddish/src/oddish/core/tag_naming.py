"""Tag key normalization, validation, and reserved-namespace checks."""
from __future__ import annotations

import re
import unicodedata


class TagNameError(ValueError):
    """Raised when a tag key / value fails normalization or validation."""


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_tag_key(raw: str) -> str:
    """Casefold, NFKC, collapse whitespace into hyphens.

    The display ``key`` keeps the user's casing; only ``normalized_key``
    is persisted into the partial-unique index.
    """
    s = unicodedata.normalize("NFKC", raw or "").strip()
    s = _WHITESPACE_RE.sub("-", s)
    return s.casefold()


def normalize_tag_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = unicodedata.normalize("NFKC", raw).strip()
    s = _WHITESPACE_RE.sub("-", s)
    return s.casefold() or None


def validate_tag_key(
    normalized: str, *, name_max_len: int, charset_re: str
) -> None:
    if not normalized:
        raise TagNameError("tag key must be non-empty")
    if len(normalized) > name_max_len:
        raise TagNameError(
            f"tag key exceeds policy max length {name_max_len}: got {len(normalized)}"
        )
    pattern = re.compile(f"^({charset_re})+$")
    if not pattern.match(normalized):
        raise TagNameError(
            f"tag key {normalized!r} contains characters outside {charset_re}"
        )


def validate_tag_value(
    normalized: str | None, *, name_max_len: int, charset_re: str
) -> None:
    if normalized is None:
        return
    if len(normalized) > name_max_len:
        raise TagNameError(
            f"tag value exceeds policy max length {name_max_len}: got {len(normalized)}"
        )
    pattern = re.compile(f"^({charset_re})+$")
    if not pattern.match(normalized):
        raise TagNameError(
            f"tag value {normalized!r} contains characters outside {charset_re}"
        )


def is_reserved_prefix(normalized_key: str, *, reserved_prefixes: list[str]) -> bool:
    """Return True if the normalized key begins with any reserved prefix."""
    return any(normalized_key.startswith(p) for p in (reserved_prefixes or []))
