"""Single-tenant GKE org allowlist.

GKE is multi-tenant-exposing: every org on the hosted product could otherwise
route TPU work to the shared cluster. ODDISH_GKE_ALLOWED_ORG_IDS is the gate --
a comma-separated allowlist, EMPTY = deny all (fail-safe default). The sweep
admission rejects a GKE-routed submission whose org is absent with a 403 before
any trial is persisted, across all three routing paths (explicit environment=gke,
TPU auto-sniff which arrives as environment=gke, and override_tpu) plus an
append's inherited GKE environment. The OSS single-tenant server (org_id None)
is exempt.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.config import Settings
from oddish.core.endpoints.sweep import _reject_gke_org_not_allowed


# --- allowlist parsing -----------------------------------------------------


def test_allowlist_default_is_empty_deny_all():
    assert Settings(gke_allowed_org_ids="").gke_allowed_org_id_set == frozenset()


def test_allowlist_parses_comma_separated_with_whitespace():
    s = Settings(gke_allowed_org_ids="org_a, org_b ,  org_c")
    assert s.gke_allowed_org_id_set == frozenset({"org_a", "org_b", "org_c"})


def test_allowlist_ignores_empty_entries():
    s = Settings(gke_allowed_org_ids="org_a,,  , org_b,")
    assert s.gke_allowed_org_id_set == frozenset({"org_a", "org_b"})


# --- the gate --------------------------------------------------------------


def test_gke_org_not_in_allowlist_rejected():
    with pytest.raises(HTTPException) as exc:
        _reject_gke_org_not_allowed(
            EnvironmentType.GKE, "org_x", frozenset({"org_allowed"})
        )
    assert exc.value.status_code == 403
    assert "not enabled" in str(exc.value.detail).lower()


def test_empty_allowlist_denies_every_gke_org():
    # Default-deny: an unset/empty allowlist rejects all orgs, even where GKE
    # config is present.
    with pytest.raises(HTTPException) as exc:
        _reject_gke_org_not_allowed(EnvironmentType.GKE, "org_x", frozenset())
    assert exc.value.status_code == 403


def test_gke_org_in_allowlist_allowed():
    _reject_gke_org_not_allowed(
        EnvironmentType.GKE, "org_x", frozenset({"org_x", "org_y"})
    )


def test_none_org_is_exempt_oss_server():
    # The OSS single-tenant server passes org_id=None; the gate is a no-op there
    # even with an empty (deny-all) allowlist.
    _reject_gke_org_not_allowed(EnvironmentType.GKE, None, frozenset())


def test_non_gke_environment_never_rejected():
    # A non-GKE submission is never gated, regardless of allowlist membership.
    _reject_gke_org_not_allowed(EnvironmentType.MODAL, "org_x", frozenset())
    _reject_gke_org_not_allowed(EnvironmentType.DAYTONA, "org_x", frozenset())
    _reject_gke_org_not_allowed(None, "org_x", frozenset())
