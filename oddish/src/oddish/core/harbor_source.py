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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harbor.models.environment_type import EnvironmentType

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
    Harbor commit the variant image baked. ``extras`` are Harbor optional-
    dependency groups the variant image must install on top of the source (the
    default image does not carry them); e.g. the gke variant needs ``("gke",)``
    for the k8s + google-cloud stack the lean default Harbor omits.
    """

    variant_id: str
    source: str
    sha: str
    extras: tuple[str, ...] = ()


# Routing id for the GKE (TPU) Harbor variant. GKE trials run the GKE-enabled
# harbor-gke fork, which the lean default Harbor (rishidesai/harbor) does not
# carry; keeping it a variant means only GKE trials pull the heavy k8s +
# google-cloud stack, on a dedicated worker image.
GKE_VARIANT_ID = "gke"

# Blessed source@sha -> image variant. Keyed by ``variant_id``. Blessing a pin
# means adding an entry here and building its image/Function (the Modal side
# reads this same registry). Any allowlisted pin not registered here runs
# out-of-process as ``ephemeral``.
#
# MERGE CHECKLIST: once harbor-gke's GKE support merges into the default fork,
# advance HARBOR_DEFAULT_SOURCE/SHA (config.py) + both pyproject/uv.lock pins to
# the merged commit and DELETE the gke entry below. The default image then
# carries GKE natively, GKE trials classify to ``default``, and the per-variant
# image/Function collapse away with the registry entry.
HARBOR_VARIANTS: dict[str, HarborVariant] = {
    GKE_VARIANT_ID: HarborVariant(
        variant_id=GKE_VARIANT_ID,
        source="https://github.com/abundant-ai/harbor-gke",
        sha="bfc3dc4e2210641acc16d293865495541edb7422",
        extras=("gke",),
    ),
}


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
    lowercase, so a user-typed ``Abundant-AI/*`` must still match.
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


def harbor_git_requirement(
    source: str, sha: str, *, extras: Sequence[str] | None = None
) -> str:
    """PEP 508 direct reference for a Harbor git pin: ``harbor @ git+<src>@<sha>``.

    The single source of truth for the override install requirement, shared by
    the ephemeral child's ``uv run --with``, the blessed-variant image build, and
    the in-sandbox agent pin. A leading ``git+`` on *source* is stripped first so
    the result never doubles the prefix.

    When *extras* is given, they are rendered as a PEP 508 extras group on the
    package name (``harbor[daytona,modal] @ git+<src>@<sha>``). Cloud-provider
    SDKs (daytona, modal, e2b, …) are optional Harbor extras, so the ephemeral
    child MUST request the extra matching the trial's environment or Harbor
    raises ``MissingExtraError`` at runtime when it tries to build the sandbox.
    Extras are sorted and de-duplicated for a stable requirement string.
    """
    name = "harbor"
    if extras:
        unique = sorted({e.strip() for e in extras if e and e.strip()})
        if unique:
            name = f"harbor[{','.join(unique)}]"
    return f"{name} @ git+{_strip_git_prefix(source)}@{sha}"


def harbor_variant_function_name(variant_id: str) -> str:
    """Modal Function name for a blessed variant's single-job worker."""
    return f"process_single_job__{variant_id}"


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


def stamp_gke_harbor_source(
    harbor: HarborConfig, environment: EnvironmentType
) -> HarborConfig:
    """Bind a GKE trial to the harbor-gke fork unless it pins a different fork.

    harbor-gke is the only Harbor carrying the GKE environment, so a trial routed
    to GKE MUST run it. When *environment* is GKE and the submission either pinned
    no source OR pinned the DEFAULT fork, stamp the blessed gke variant's
    ``(source, sha)`` — the sha rides as the ref so resolution needs no network and
    the pin classifies deterministically onto the gke worker image. The default
    fork is treated exactly like an unset source because it carries no GKE support
    (see the merge checklist above); leaving a GKE trial on it would silently run
    the lean default image. Only a genuinely different explicit fork is left
    untouched (the allowlist gates it; a non-merge-sha GKE source runs
    out-of-process, which installs ``harbor[gke]`` in its own child). Every non-GKE
    environment is returned unchanged, so a non-GKE trial never resolves to
    harbor-gke.
    """
    if environment != EnvironmentType.GKE:
        return harbor
    # A pin of the default fork is treated like an unset source (see docstring):
    # only a genuinely different fork is left for out-of-process resolution.
    if harbor.source is not None and _normalize_source(
        harbor.source
    ) != _normalize_source(HARBOR_DEFAULT_SOURCE):
        return harbor
    variant = HARBOR_VARIANTS[GKE_VARIANT_ID]
    return harbor.model_copy(update={"source": variant.source, "ref": variant.sha})


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
