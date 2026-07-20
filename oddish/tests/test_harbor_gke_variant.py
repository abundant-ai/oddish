"""The blessed GKE Harbor variant: registry pin, classification, and submission
source-stamping.

GKE (TPU) trials run the GKE-enabled harbor-gke fork on a dedicated blessed
worker image; every other trial stays on the lean default Harbor. These tests
pin that decoupling: a GKE trial classifies to the ``gke`` variant, a non-GKE
trial stays ``default``, and an explicit caller override is never overwritten.
"""

from __future__ import annotations

import re

from harbor.models.environment_type import EnvironmentType

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE, Settings
from oddish.core.harbor_source import (
    GKE_VARIANT_ID,
    HARBOR_VARIANTS,
    classify_variant,
    resolve_and_gate_harbor,
    resolve_harbor_pin,
    stamp_gke_harbor_source,
)
from oddish.schemas import HarborConfig

GKE_HARBOR_SOURCE = "https://github.com/abundant-ai/harbor-gke"
GKE_HARBOR_SHA = "196c2e47157738e9f4390dda90fe76b6247b2ff2"


def test_gke_variant_registered_with_expected_pin():
    # Drift guard: the blessed gke variant points at harbor-gke@merge-sha and
    # requests harbor's gke extra (k8s + google-cloud) for its image.
    v = HARBOR_VARIANTS[GKE_VARIANT_ID]
    assert v.variant_id == "gke"
    assert v.source == GKE_HARBOR_SOURCE
    assert v.sha == GKE_HARBOR_SHA
    assert re.fullmatch(r"[0-9a-f]{40}", v.sha)
    assert v.extras == ("gke",)


def test_gke_variant_is_not_the_reverted_default():
    # The whole point of the decoupling: harbor-gke is a distinct variant, not
    # the default the lean image bakes.
    assert HARBOR_DEFAULT_SOURCE == "https://github.com/abundant-ai/harbor"
    assert HARBOR_VARIANTS[GKE_VARIANT_ID].source != HARBOR_DEFAULT_SOURCE


def test_gke_source_classifies_to_gke_variant():
    v = HARBOR_VARIANTS[GKE_VARIANT_ID]
    assert classify_variant(v.source, v.sha) == GKE_VARIANT_ID
    # git+ and case spellings normalize to the same variant.
    assert classify_variant(f"git+{v.source}", v.sha) == GKE_VARIANT_ID
    assert classify_variant(v.source.upper(), v.sha) == GKE_VARIANT_ID


def test_gke_source_at_other_sha_is_ephemeral_not_gke():
    # Only the exact merge sha is the blessed image; another harbor-gke commit
    # runs out-of-process (ephemeral), never mislabeled onto the gke image.
    assert classify_variant(GKE_HARBOR_SOURCE, "a" * 40) == "ephemeral"


def test_stamp_gke_source_on_gke_env_without_override():
    stamped = stamp_gke_harbor_source(HarborConfig(), EnvironmentType.GKE)
    v = HARBOR_VARIANTS[GKE_VARIANT_ID]
    assert stamped.source == v.source
    # The ref is stamped as the 40-hex sha so resolution needs no network and
    # the pin classifies deterministically to the gke variant.
    assert stamped.ref == v.sha
    pin = resolve_harbor_pin(stamped.source, stamped.ref)
    assert classify_variant(pin.source, pin.sha) == GKE_VARIANT_ID


def test_stamp_leaves_non_gke_env_on_default():
    # Decoupling completeness: no non-GKE environment is ever pointed at
    # harbor-gke. Each keeps the default (source None -> locked default).
    for env in (
        EnvironmentType.MODAL,
        EnvironmentType.DAYTONA,
        EnvironmentType.DOCKER,
        EnvironmentType.E2B,
    ):
        stamped = stamp_gke_harbor_source(HarborConfig(), env)
        assert stamped.source is None
        assert stamped.ref is None


def test_stamp_respects_explicit_caller_override_on_gke():
    # A caller who pinned an explicit source keeps it (the allowlist gates it);
    # an override that isn't the merge sha runs ephemeral, which installs
    # harbor[gke] in its own child.
    hc = HarborConfig(source="https://github.com/dot-agi/harbor", ref="main")
    stamped = stamp_gke_harbor_source(hc, EnvironmentType.GKE)
    assert stamped.source == "https://github.com/dot-agi/harbor"
    assert stamped.ref == "main"


def test_stamp_treats_explicit_default_fork_pin_like_unset_on_gke():
    # A GKE trial that explicitly pins the DEFAULT fork (e.g. --harbor
    # abundant-ai/harbor) must STILL be redirected to harbor-gke: the default fork
    # carries no GKE support, so honoring it would silently run GKE on the lean
    # default image. Treated exactly like source=None, and normalized so git+ /
    # case spellings of the default are caught too.
    v = HARBOR_VARIANTS[GKE_VARIANT_ID]
    for hc in (
        HarborConfig(source=HARBOR_DEFAULT_SOURCE),
        HarborConfig(source=HARBOR_DEFAULT_SOURCE, ref=HARBOR_DEFAULT_SHA),
        HarborConfig(source=f"git+{HARBOR_DEFAULT_SOURCE}"),
        HarborConfig(source=HARBOR_DEFAULT_SOURCE.upper()),
    ):
        stamped = stamp_gke_harbor_source(hc, EnvironmentType.GKE)
        assert stamped.source == v.source
        assert stamped.ref == v.sha
        pin = resolve_harbor_pin(stamped.source, stamped.ref)
        assert classify_variant(pin.source, pin.sha) == GKE_VARIANT_ID


def test_stamp_leaves_genuinely_different_fork_untouched_on_gke():
    # Only the exact default repo is treated as "unset"; a genuinely different
    # fork -- even one under the same owner -- is honored and runs out-of-process
    # (its ephemeral child installs harbor[gke]).
    for src in (
        "https://github.com/dot-agi/harbor",
        "https://github.com/abundant-ai/harbor-fork",
    ):
        hc = HarborConfig(source=src, ref="main")
        stamped = stamp_gke_harbor_source(hc, EnvironmentType.GKE)
        assert stamped.source == src
        assert stamped.ref == "main"


def test_stamp_leaves_default_fork_pin_untouched_on_non_gke():
    # The default-fork == unset rule is GKE-only: no non-GKE environment is ever
    # redirected, even when it explicitly pins the default fork.
    hc = HarborConfig(source=HARBOR_DEFAULT_SOURCE, ref=HARBOR_DEFAULT_SHA)
    for env in (
        EnvironmentType.MODAL,
        EnvironmentType.DAYTONA,
        EnvironmentType.DOCKER,
    ):
        stamped = stamp_gke_harbor_source(hc, env)
        assert stamped.source == HARBOR_DEFAULT_SOURCE
        assert stamped.ref == HARBOR_DEFAULT_SHA


def test_gke_stamped_submission_gates_to_gke_variant():
    # End-to-end: a GKE default submission stamps to harbor-gke, passes the
    # allowlist, and resolves to the gke variant id on the trial's harbor config.
    stamped_pre = stamp_gke_harbor_source(HarborConfig(), EnvironmentType.GKE)
    stamped, variant = resolve_and_gate_harbor(stamped_pre, settings=Settings())
    assert variant == GKE_VARIANT_ID
    assert stamped.variant_id == GKE_VARIANT_ID
    assert stamped.resolved_sha == GKE_HARBOR_SHA


def test_default_submission_still_gates_to_default():
    # A non-GKE (unstamped) default submission stays on the default variant.
    stamped, variant = resolve_and_gate_harbor(HarborConfig(), settings=Settings())
    assert variant == "default"
    assert stamped.resolved_sha == HARBOR_DEFAULT_SHA
