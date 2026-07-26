from __future__ import annotations

import os

from fastapi import HTTPException

from auth.types import AuthContext
from auth.types import AuthMethod
from models import APIKeyScope
from models import UserRole


def is_operator_org(auth: AuthContext) -> bool:
    """Whether the caller's active org is the platform-operator org.

    ``ODDISH_OPERATOR_ORG_ID`` names the operator org. By default it is matched
    against the org's internal **id** (server-issued, not caller-controllable).
    Prefix it with ``slug:`` to match the org's human-readable **slug** instead
    -- e.g. ``slug:abundant`` -- matched case-insensitively. An unset/blank
    value grants operator access to no one.

    The id and slug forms are kept separate on purpose. Matching a bare value
    against both would let a tenant that claims an operator's id-string as its
    own ``org_slug`` (which the org's creator chooses) pass the check. Requiring
    an explicit ``slug:`` opt-in for slug matching closes that escalation.
    """
    configured = os.environ.get("ODDISH_OPERATOR_ORG_ID", "").strip()
    if not configured:
        return False
    if configured.lower().startswith("slug:"):
        want = configured[len("slug:") :].strip().lower()
        slug = (getattr(auth, "org_slug", None) or "").strip().lower()
        return bool(want) and slug == want
    return getattr(auth, "org_id", None) == configured


def require_operator_org(auth: AuthContext) -> None:
    if not is_operator_org(auth):
        raise HTTPException(status_code=403, detail="Operator access required")


def assert_org_access(row: object, auth: AuthContext, *, detail: str) -> None:
    """Re-check a row resolved from a caller-supplied id against the caller's org.

    Lookups that accept an opaque id resolve across scopes -- an id names any
    row in the installation, not just the caller's -- so every id-resolved row
    needs this before it is read or written. A row with no ``org_id`` is the
    installation-wide default and is visible to everyone.

    404 rather than 403: a foreign row's *existence* is itself not the caller's
    to learn.
    """
    org_id = getattr(row, "org_id", None)
    if org_id and org_id != auth.org_id:
        raise HTTPException(status_code=404, detail=detail)


def can_create_api_keys(auth: AuthContext) -> bool:
    """Return whether this user may create organization API keys.

    Any org ADMIN or MEMBER qualifies (self-service for every org, gated on the
    caller's role in their current org). API key auth never qualifies so one key
    cannot mint another.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        return False

    role = auth.user.role if auth.user else auth.user_role
    return role in {UserRole.ADMIN, UserRole.MEMBER}


def can_manage_api_keys(auth: AuthContext) -> bool:
    """Return whether this user may list/revoke all organization API keys."""
    if auth.method != AuthMethod.CLERK_JWT:
        return False
    role = auth.user.role if auth.user else auth.user_role
    return role == UserRole.ADMIN


def allowed_api_key_scopes(auth: AuthContext) -> list[APIKeyScope]:
    """Return scopes the current user may mint."""
    if not can_create_api_keys(auth):
        return []
    role = auth.user.role if auth.user else auth.user_role
    if role == UserRole.ADMIN:
        return [APIKeyScope.FULL, APIKeyScope.TASKS, APIKeyScope.READ]
    return [APIKeyScope.TASKS, APIKeyScope.READ]


def can_manage_quotas(auth: AuthContext) -> bool:
    """Return whether this user may view/set org member quotas.

    Any org ADMIN qualifies (self-service for every org). API keys never qualify
    -- quota management is user-auth-only, enforced by require_can_manage_quotas.
    """
    role = auth.user.role if auth.user else auth.user_role
    return role == UserRole.ADMIN
