"""Single-tenant GKE user allowlist.

GKE is exposed only to explicitly allowlisted USERS, keyed on the authenticated
user's email (ODDISH_GKE_ALLOWED_USER_EMAILS; comma-separated, case-insensitive,
EMPTY = deny all). The email is server-derived and unforgeable: for Clerk-JWT
auth it is the session user's email, and for an API key it is the key CREATOR's
email. The sweep admission rejects a GKE-routed hosted submission whose email is
absent (or None, e.g. a key with no creator user) with a 403 before any trial is
persisted, across all three routing paths. The OSS single-tenant server
(org_id None, unauthenticated) is exempt.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.config import Settings
from oddish.core.endpoints.sweep import _reject_gke_user_not_allowed

WHITELIST = frozenset({"pratty@abundant.ai", "rishi@abundant.ai"})


# --- allowlist parsing -----------------------------------------------------


def test_allowlist_default_is_empty_deny_all():
    assert Settings(gke_allowed_user_emails="").gke_allowed_user_email_set == frozenset()


def test_allowlist_parses_comma_separated_lowercased():
    s = Settings(gke_allowed_user_emails="Pratty@Abundant.AI, rishi@abundant.ai ")
    assert s.gke_allowed_user_email_set == frozenset(
        {"pratty@abundant.ai", "rishi@abundant.ai"}
    )


def test_allowlist_ignores_blank_entries():
    s = Settings(gke_allowed_user_emails="a@x.com,, , b@x.com,")
    assert s.gke_allowed_user_email_set == frozenset({"a@x.com", "b@x.com"})


# --- the gate --------------------------------------------------------------


def test_gke_email_not_in_allowlist_rejected():
    with pytest.raises(HTTPException) as exc:
        _reject_gke_user_not_allowed(
            EnvironmentType.GKE, "org1", "mallory@evil.com", WHITELIST
        )
    assert exc.value.status_code == 403
    assert "not enabled" in str(exc.value.detail).lower()


def test_gke_email_in_allowlist_allowed():
    _reject_gke_user_not_allowed(EnvironmentType.GKE, "org1", "rishi@abundant.ai", WHITELIST)


def test_gke_email_match_is_case_insensitive():
    _reject_gke_user_not_allowed(
        EnvironmentType.GKE, "org1", "Pratty@Abundant.AI", WHITELIST
    )


def test_hosted_none_email_is_denied_default_deny():
    # A hosted request (org_id set) whose user identity can't be resolved
    # (e.g. an API key with no creator user -> user_email None) must be denied,
    # NOT treated like the OSS exemption.
    with pytest.raises(HTTPException) as exc:
        _reject_gke_user_not_allowed(EnvironmentType.GKE, "org1", None, WHITELIST)
    assert exc.value.status_code == 403


def test_empty_allowlist_denies_every_hosted_user():
    with pytest.raises(HTTPException):
        _reject_gke_user_not_allowed(
            EnvironmentType.GKE, "org1", "rishi@abundant.ai", frozenset()
        )


def test_oss_unauthenticated_is_exempt():
    # OSS single-tenant server: org_id None (unauthenticated). Exempt even with an
    # empty allowlist and a None email.
    _reject_gke_user_not_allowed(EnvironmentType.GKE, None, None, frozenset())


def test_non_gke_environment_never_rejected():
    _reject_gke_user_not_allowed(EnvironmentType.MODAL, "org1", "mallory@evil.com", frozenset())
    _reject_gke_user_not_allowed(EnvironmentType.DAYTONA, "org1", None, frozenset())
    _reject_gke_user_not_allowed(None, "org1", "mallory@evil.com", frozenset())
