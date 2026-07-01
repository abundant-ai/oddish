from __future__ import annotations

from auth.types import AuthContext
from models import UserRole

API_KEY_CREATOR_EMAIL_DOMAIN = "@abundant.ai"
API_KEY_CREATOR_ORG_SLUGS = frozenset(
    {"abundant", "abundant-ai", "abundant-1771551017"}
)
API_KEY_CREATOR_CLERK_ORG_IDS = frozenset({"org_39ufkEqie8rLlVhoK4YMm4IMx0L"})


def _normalized_user_email(auth: AuthContext) -> str:
    email = auth.user.email if auth.user else auth.user_email
    return (email or "").strip().lower()


def _normalized_org_slug(auth: AuthContext) -> str:
    slug = auth.org.slug if auth.org else auth.org_slug
    return (slug or "").strip().lower()


def _normalized_clerk_org_id(auth: AuthContext) -> str:
    clerk_org_id = auth.org.clerk_org_id if auth.org else None
    return (clerk_org_id or "").strip()


def can_create_api_keys(auth: AuthContext) -> bool:
    """Return whether this user may create organization API keys.

    Only admins with an @abundant.ai email in the main Abundant org qualify.
    """
    role = auth.user.role if auth.user else auth.user_role
    if role != UserRole.ADMIN:
        return False

    return _normalized_user_email(auth).endswith(API_KEY_CREATOR_EMAIL_DOMAIN) and (
        _normalized_org_slug(auth) in API_KEY_CREATOR_ORG_SLUGS
        or _normalized_clerk_org_id(auth) in API_KEY_CREATOR_CLERK_ORG_IDS
    )
