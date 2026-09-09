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


def _truthy_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return False
    return normalized in {"1", "true", "yes", "on"}


def _configured_spend_orgs() -> list[str]:
    raw = os.environ.get("ODDISH_APPROVED_SPEND_ORGS", "")
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def _org_ref_matches(auth: AuthContext, ref: str) -> bool:
    """Match an approved spend org reference against the active auth org.

    Bare values are internal org ids only. Use ``slug:<slug>`` when operators
    want to approve by human-readable slug; this mirrors ``ODDISH_OPERATOR_ORG_ID``
    and avoids letting a user-created slug impersonate a server-issued org id.
    """
    normalized = ref.strip()
    if not normalized:
        return False
    if normalized.lower().startswith("slug:"):
        want = normalized[len("slug:") :].strip().lower()
        got = (auth.org_slug or "").strip().lower()
        return bool(want) and got == want
    if normalized.lower().startswith("id:"):
        normalized = normalized[len("id:") :].strip()
    return bool(normalized) and auth.org_id == normalized


def spend_org_approval_required() -> bool:
    """Whether paid-spend entrypoints require an approved org.

    Explicit ``ODDISH_REQUIRE_APPROVED_SPEND_ORG`` wins. Otherwise the gate
    turns on automatically once the deployment has an operator org or any
    approved spend orgs configured. Local/self-hosted runs with neither remain
    open by default.
    """
    explicit = _truthy_env("ODDISH_REQUIRE_APPROVED_SPEND_ORG")
    if explicit is not None:
        return explicit
    return bool(
        os.environ.get("ODDISH_OPERATOR_ORG_ID", "").strip()
        or _configured_spend_orgs()
    )


def is_approved_spend_org(auth: AuthContext) -> bool:
    """Return whether the active org may initiate platform-funded spend."""
    if not spend_org_approval_required():
        return True
    if is_operator_org(auth):
        return True
    return any(_org_ref_matches(auth, ref) for ref in _configured_spend_orgs())


def require_approved_spend_org(auth: AuthContext) -> None:
    if not is_approved_spend_org(auth):
        raise HTTPException(
            status_code=403,
            detail=(
                "This organization is not approved to create platform-funded "
                "runs. Ask an operator to approve the org or use an approved "
                "workspace."
            ),
        )


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

    Any ADMIN or MEMBER in an approved spend org qualifies. API key auth never
    qualifies so one key cannot mint another.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        return False
    if not is_approved_spend_org(auth):
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

    Any org ADMIN qualifies (self-service for every org). API key auth never
    qualifies -- quota management is user-auth-only.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        return False
    role = auth.user.role if auth.user else auth.user_role
    return role == UserRole.ADMIN
