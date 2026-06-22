"""Server-side Harbor source resolution, allowlist, and variant classification.

Resolves a submission's ``(source, ref)`` to a concrete commit SHA, enforces the
allowlist, and classifies the pin into an execution variant (``default`` ->
in-process baked Harbor, a registered ``<id>`` -> blessed image, ``ephemeral`` ->
out-of-process child). Raises plain exceptions; the FastAPI layer translates them
to HTTP 422.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE

if TYPE_CHECKING:
    from oddish.config import Settings
    from oddish.schemas import HarborConfig

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class HarborVariant:
    """A blessed Harbor pin that has a dedicated, pre-baked worker image.

    ``variant_id`` is the routing id stored on the trial / worker job and used to
    select the per-variant Modal Function. ``source`` / ``sha`` identify the exact
    Harbor commit the variant image baked.
    """

    variant_id: str
    source: str
    sha: str


# Blessed source@sha -> image variant. Keyed by ``variant_id``. Empty by default;
# blessing a pin means adding an entry here and building its image/Function (the
# Modal side reads this same registry). Any allowlisted pin not registered here
# runs out-of-process as ``ephemeral``.
HARBOR_VARIANTS: dict[str, HarborVariant] = {}


class HarborSourceError(Exception):
    """Disallowed source or a ref that could not be resolved."""


@dataclass(frozen=True)
class ResolvedPin:
    source: str
    sha: str


def _strip_git_prefix(source: str) -> str:
    """Strip a leading ``git+`` so the bare git URL is used everywhere.

    A spec may arrive as ``git+https://…`` (R1). The bare URL is what
    ``git ls-remote`` wants and what keeps the install requirement
    ``harbor @ git+<source>@<sha>`` from doubling the prefix.
    """
    s = source.strip()
    if s.startswith("git+"):
        s = s[len("git+") :]
    return s


def _normalize_source(source: str) -> str:
    """Lowercase + strip a leading ``git+`` so matching is case/prefix stable."""
    return _strip_git_prefix(source).lower()


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
    remote HEAD. A leading ``git+`` on *source* is stripped so the stored pin and
    every downstream install requirement use the bare git URL.
    """
    source = _strip_git_prefix(source)
    ref = (ref or "").strip()
    if _SHA_RE.match(ref):
        return ResolvedPin(source, ref)

    # ``--`` terminates options so a '-'-prefixed source can't be read as a flag.
    cmd = ["git", "ls-remote", "--", source] + ([ref] if ref else ["HEAD"])
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


def harbor_git_requirement(source: str, sha: str) -> str:
    """PEP 508 direct reference for a Harbor git pin: ``harbor @ git+<src>@<sha>``.

    The single source of truth for the override install requirement, shared by
    the ephemeral child's ``uv run --with``, the blessed-variant image build, and
    the in-sandbox agent pin. A leading ``git+`` on *source* is stripped first so
    the result never doubles the prefix.
    """
    return f"harbor @ git+{_strip_git_prefix(source)}@{sha}"


def harbor_variant_function_name(variant_id: str) -> str:
    """Modal Function name for a blessed variant's single-job worker."""
    return f"process_single_job__{variant_id}"


# A fixed python program (no interpolation -> no shell-injection surface) that
# repoints the ``[tool.uv.sources]`` harbor inline-table at ``argv[1]@argv[2]``
# for every pyproject path in ``argv[3:]``. Used in a blessed-variant image build
# to rewrite the harbor pin BEFORE ``uv_sync`` so the whole dependency set
# resolves against the variant's commit.
_HARBOR_SOURCE_REWRITE_PY = (
    "import re,sys\n"
    "src,sha=sys.argv[1],sys.argv[2]\n"
    "for p in sys.argv[3:]:\n"
    '    s=open(p).read()\n'
    '    s=re.sub(r"harbor = \\{ git = [^}]*\\}", '
    '"harbor = { git = \\""+src+"\\", rev = \\""+sha+"\\" }", s)\n'
    '    open(p,"w").write(s)'
)


def harbor_uv_source_rewrite_command(
    source: str, sha: str, *pyproject_paths: str
) -> str:
    """Shell command repointing the harbor ``[tool.uv.sources]`` pin in-place.

    Emits ``python3 -c '<fixed program>' <source> <sha> <paths...>`` (python is
    always present in the image; this is portable where ``sed -i`` is not). The
    program is fixed and the dynamic values ride as argv, so there's no
    interpolation/injection surface.
    """
    src = _strip_git_prefix(source)
    args = " ".join([src, sha, *pyproject_paths])
    return f"python3 -c '{_HARBOR_SOURCE_REWRITE_PY}' {args}"


def classify_variant(source: str, sha: str) -> str:
    """Return the routing id: 'default' | '<registry-id>' | 'ephemeral'.

    The source is compared NORMALIZED (lowercased, leading ``git+`` stripped) on
    both sides so ``git+`` / case spellings of the same repo classify alike — the
    locked default URL is lowercase, and the registry is matched the same way.
    """
    norm = _normalize_source(source)
    if norm == _normalize_source(HARBOR_DEFAULT_SOURCE) and sha == HARBOR_DEFAULT_SHA:
        return "default"
    for variant in HARBOR_VARIANTS.values():
        if _normalize_source(variant.source) == norm and variant.sha == sha:
            return variant.variant_id
    return "ephemeral"


def resolve_and_gate_harbor(
    harbor: HarborConfig,
    *,
    settings: Settings,
) -> tuple[HarborConfig, str]:
    """Resolve the (source, ref) on *harbor* to a SHA, classify it, and stamp it.

    Returns ``(harbor_with_resolved_sha_and_variant_id, variant_id)``. The only
    gate is the allowlist: a non-default source not in
    ``ODDISH_HARBOR_ALLOWED_SOURCES`` (or an unresolvable ref) raises
    ``HarborSourceError`` (the FastAPI layer maps it to HTTP 422). The default pin
    does NO network I/O (40-hex short-circuit in resolve_harbor_pin).
    """
    source = harbor.source or HARBOR_DEFAULT_SOURCE
    ref = harbor.ref if harbor.ref is not None else HARBOR_DEFAULT_SHA

    is_default_request = harbor.source is None and (
        harbor.ref is None or harbor.ref == HARBOR_DEFAULT_SHA
    )
    if not is_default_request:
        assert_allowed(source, allowed=settings.harbor_allowed_sources)

    pin = resolve_harbor_pin(source, ref)
    variant = classify_variant(pin.source, pin.sha)

    stamped = harbor.model_copy(
        update={"source": pin.source, "resolved_sha": pin.sha, "variant_id": variant}
    )
    return stamped, variant
