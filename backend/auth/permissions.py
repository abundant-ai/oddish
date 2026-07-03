from __future__ import annotations

from auth.types import AuthContext
from auth.types import AuthMethod
from models import APIKeyScope
from models import UserRole

API_KEY_CREATOR_ORG_SLUGS = frozenset(
    {"abundant", "abundant-ai", "abundant-1771551017"}
)
API_KEY_CREATOR_CLERK_ORG_IDS = frozenset({"org_39ufkEqie8rLlVhoK4YMm4IMx0L"})


def _normalized_org_slug(auth: AuthContext) -> str:
    slug = auth.org.slug if auth.org else auth.org_slug
    return (slug or "").strip().lower()


def _normalized_clerk_org_id(auth: AuthContext) -> str:
    clerk_org_id = auth.org.clerk_org_id if auth.org else None
    return (clerk_org_id or "").strip()


def can_create_api_keys(auth: AuthContext) -> bool:
    """Return whether this user may create organization API keys.

    Admins and members in the main Abundant org qualify. API key auth never
    qualifies so one key cannot mint another.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        return False

    role = auth.user.role if auth.user else auth.user_role
    if role not in {UserRole.ADMIN, UserRole.MEMBER}:
        return False

    return (
        _normalized_org_slug(auth) in API_KEY_CREATOR_ORG_SLUGS
        or _normalized_clerk_org_id(auth) in API_KEY_CREATOR_CLERK_ORG_IDS
    )


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
