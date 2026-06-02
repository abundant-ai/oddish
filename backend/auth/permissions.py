from __future__ import annotations

from auth.types import AuthContext
from models import UserRole

API_KEY_CREATOR_EMAIL_DOMAIN = "@abundant.ai"
API_KEY_CREATOR_ORG_SLUGS = frozenset({"abundant", "abundant-ai"})


def _normalized_user_email(auth: AuthContext) -> str:
    email = auth.user.email if auth.user else auth.user_email
    return (email or "").strip().lower()


def _normalized_org_slug(auth: AuthContext) -> str:
    slug = auth.org.slug if auth.org else auth.org_slug
    return (slug or "").strip().lower()


def can_create_api_keys(auth: AuthContext) -> bool:
    """Return whether this user may create organization API keys."""
    role = auth.user.role if auth.user else auth.user_role
    if role == UserRole.OWNER:
        return True

    if role != UserRole.ADMIN:
        return False

    return (
        _normalized_user_email(auth).endswith(API_KEY_CREATOR_EMAIL_DOMAIN)
        and _normalized_org_slug(auth) in API_KEY_CREATOR_ORG_SLUGS
    )
