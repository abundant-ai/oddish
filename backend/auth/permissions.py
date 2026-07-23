from __future__ import annotations

import os

from fastapi import HTTPException

from auth.types import AuthContext
from auth.types import AuthMethod
from models import APIKeyScope
from models import UserRole


def is_operator_org(auth: AuthContext) -> bool:
    operator_org_id = os.environ.get("ODDISH_OPERATOR_ORG_ID", "").strip()
    return bool(operator_org_id and auth.org_id == operator_org_id)


def require_operator_org(auth: AuthContext) -> None:
    if not is_operator_org(auth):
        raise HTTPException(status_code=403, detail="Operator access required")


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
