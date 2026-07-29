"""Unit tests: quota/API-key predicates reject API-key auth themselves."""

from __future__ import annotations

from auth.permissions import (
    can_create_api_keys,
    can_manage_api_keys,
    can_manage_quotas,
)
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole


def _clerk_auth(*, role: UserRole) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="user_1",
        user_role=role,
    )


def _admin_creator_api_key_auth() -> AuthContext:
    # FULL-scope key minted by an admin: user_role is the creator's role.
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="creator_1",
        user_role=UserRole.ADMIN,
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.FULL,
    )


def _anonymous_auth() -> AuthContext:
    return AuthContext(method=AuthMethod.ANONYMOUS)


def test_can_manage_quotas_rejects_admin_creator_api_key():
    assert can_manage_quotas(_admin_creator_api_key_auth()) is False


def test_can_manage_quotas_allows_clerk_admin():
    assert can_manage_quotas(_clerk_auth(role=UserRole.ADMIN)) is True


def test_can_manage_quotas_rejects_clerk_member():
    assert can_manage_quotas(_clerk_auth(role=UserRole.MEMBER)) is False


def test_can_manage_quotas_rejects_anonymous():
    assert can_manage_quotas(_anonymous_auth()) is False


def test_can_create_api_keys_matches_method_guard():
    assert can_create_api_keys(_admin_creator_api_key_auth()) is False
    assert can_create_api_keys(_clerk_auth(role=UserRole.ADMIN)) is True
    assert can_create_api_keys(_clerk_auth(role=UserRole.MEMBER)) is True
    assert can_create_api_keys(_anonymous_auth()) is False


def test_can_manage_api_keys_matches_method_guard():
    assert can_manage_api_keys(_admin_creator_api_key_auth()) is False
    assert can_manage_api_keys(_clerk_auth(role=UserRole.ADMIN)) is True
    assert can_manage_api_keys(_clerk_auth(role=UserRole.MEMBER)) is False
    assert can_manage_api_keys(_anonymous_auth()) is False
