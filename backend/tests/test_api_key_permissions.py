from fastapi import HTTPException

from auth.permissions import allowed_api_key_scopes, can_create_api_keys
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole


def _clerk_auth(
    *,
    role: UserRole,
    org_slug: str = "abundant",
    clerk_org_id: str | None = None,
    email: str = "member@example.com",
) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        org_slug=org_slug,
        user_id="user_1",
        user_email=email,
        user_role=role,
    )


def test_abundant_member_can_create_limited_api_keys_without_abundant_email():
    auth = _clerk_auth(role=UserRole.MEMBER, email="contractor@example.com")

    assert can_create_api_keys(auth) is True
    assert allowed_api_key_scopes(auth) == [APIKeyScope.TASKS, APIKeyScope.READ]


def test_abundant_admin_can_create_full_api_keys_without_abundant_email():
    auth = _clerk_auth(role=UserRole.ADMIN, email="admin@example.com")

    assert can_create_api_keys(auth) is True
    assert allowed_api_key_scopes(auth) == [
        APIKeyScope.FULL,
        APIKeyScope.TASKS,
        APIKeyScope.READ,
    ]


def test_non_abundant_member_cannot_create_api_keys():
    auth = _clerk_auth(role=UserRole.MEMBER, org_slug="customer")

    assert can_create_api_keys(auth) is False
    assert allowed_api_key_scopes(auth) == []


def test_api_key_auth_cannot_create_more_api_keys():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        org_slug="abundant",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.TASKS,
    )

    assert can_create_api_keys(auth) is False
    assert allowed_api_key_scopes(auth) == []


def test_member_created_task_key_is_blocked_from_restricted_task_operations():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="user_1",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.TASKS,
    )

    # Default TASKS-scope checks include medium-tier actions such as cancellation.
    auth.require_scope(APIKeyScope.TASKS)

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "Member-created API keys" in exc.detail
    else:
        raise AssertionError("member-created task key should be restricted")


def test_admin_created_task_key_can_use_restricted_task_operations():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
        scope=APIKeyScope.TASKS,
    )

    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)


def test_task_key_with_unknown_creator_role_is_restricted():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="deleted_user_1",
        user_role=None,
        scope=APIKeyScope.TASKS,
    )

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("task key with unknown creator role should be restricted")
