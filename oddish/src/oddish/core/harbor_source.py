"""Server-side Harbor source resolution, allowlist, and the Phase-A gate.

Phase A: resolve (source, ref) -> concrete SHA, classify the variant, and
reject any non-default pin at submit until the execution engines (Phases B/C)
land. Raises plain exceptions; the FastAPI layer translates them to HTTP 422.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass


class HarborSourceError(Exception):
    """Disallowed source or a ref that could not be resolved."""


class HarborOverrideDisabledError(Exception):
    """A non-default pin was requested while overrides are gated off."""


@dataclass(frozen=True)
class ResolvedPin:
    source: str
    sha: str


def _normalize_source(source: str) -> str:
    """Lowercase + strip a leading ``git+`` so matching/templating is stable."""
    s = source.strip()
    if s.startswith("git+"):
        s = s[len("git+") :]
    return s.lower()


def assert_allowed(source: str, *, allowed: str) -> None:
    """Raise ``HarborSourceError`` unless *source* matches an allowlist glob.

    Both the source and every glob are case-insensitively normalised (lowercase,
    leading ``git+`` stripped) before matching — the locked default URL is
    lowercase ``rishidesai``, so a user-typed ``RishiDesai/*`` must still match.
    """
    norm = _normalize_source(source)
    globs = [_normalize_source(g) for g in allowed.split(",") if g.strip()]
    if any(fnmatch.fnmatch(norm, g) for g in globs):
        return
    raise HarborSourceError(
        f"Harbor source {source!r} is not in ODDISH_HARBOR_ALLOWED_SOURCES ({allowed!r})"
    )
