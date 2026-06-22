"""Server-side Harbor source resolution, allowlist, and the Phase-A gate.

Phase A: resolve (source, ref) -> concrete SHA, classify the variant, and
reject any non-default pin at submit until the execution engines (Phases B/C)
land. Raises plain exceptions; the FastAPI layer translates them to HTTP 422.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Phase B fills this registry (blessed source@sha -> image variant id). Empty in
# Phase A, so every non-default pin classifies as "ephemeral".
HARBOR_VARIANTS: dict[tuple[str, str], str] = {}


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


def resolve_harbor_pin(source: str, ref: str) -> ResolvedPin:
    """Resolve ``(source, ref)`` to a concrete commit SHA.

    A 40-hex ``ref`` is treated as already-resolved and short-circuits WITHOUT
    any network I/O (this is the zero-latency default path). Any other ref is
    resolved via ``git ls-remote <source> <ref>``; an empty ``ref`` resolves the
    remote HEAD.
    """
    ref = (ref or "").strip()
    if _SHA_RE.match(ref):
        return ResolvedPin(source, ref)

    cmd = ["git", "ls-remote", source] + ([ref] if ref else ["HEAD"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarborSourceError(
            f"git ls-remote failed for {source}@{ref}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise HarborSourceError(
            f"git ls-remote {source}@{ref} exited {proc.returncode}: "
            f"{proc.stderr.strip()[:300]}"
        )
    first = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    sha = first.split("\t", 1)[0].strip() if first else ""
    if not _SHA_RE.match(sha):
        raise HarborSourceError(f"Could not resolve {source}@{ref!r} to a commit SHA")
    return ResolvedPin(source, sha)


def classify_variant(source: str, sha: str) -> str:
    """Return the routing id: 'default' | '<registry-id>' | 'ephemeral'."""
    if source == HARBOR_DEFAULT_SOURCE and sha == HARBOR_DEFAULT_SHA:
        return "default"
    variant = HARBOR_VARIANTS.get((source, sha))  # empty in Phase A
    return variant if variant is not None else "ephemeral"
