from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class _UserRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"


class _AuthMethod(str, Enum):
    CLERK_JWT = "clerk_jwt"
    API_KEY = "api_key"


class _APIKeyScope(str, Enum):
    FULL = "full"
    TASKS = "tasks"
    READ = "read"


@dataclass
class _UserStub:
    role: _UserRole
    email: str | None


@dataclass
class _OrgStub:
    slug: str | None
    clerk_org_id: str | None = None


@dataclass
class _AuthStub:
    org: _OrgStub | None = None
    org_slug: str | None = None
    user: _UserStub | None = None
    user_email: str | None = None
    user_role: _UserRole | None = None
    method: _AuthMethod = _AuthMethod.CLERK_JWT


def _load_permissions() -> dict[str, Any]:
    permissions_path = (
        Path(__file__).resolve().parents[2] / "backend" / "auth" / "permissions.py"
    )
    source = "\n".join(
        line
        for line in permissions_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("from auth.", "from models import"))
    )
    namespace: dict[str, Any] = {
        "AuthContext": _AuthStub,
        "UserRole": _UserRole,
        "AuthMethod": _AuthMethod,
        "APIKeyScope": _APIKeyScope,
    }
    exec(source, namespace)
    return namespace


def test_org_admin_can_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="customer-org"),
        user=_UserStub(role=_UserRole.ADMIN, email="admin@example.com"),
    )

    assert can_create_api_keys(auth) is True


def test_org_member_can_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="customer-org"),
        user=_UserStub(role=_UserRole.MEMBER, email="member@example.com"),
    )

    assert can_create_api_keys(auth) is True


def test_api_key_creation_is_org_agnostic() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    for slug in ("abundant", "customer-org", "acme-labs"):
        auth = _AuthStub(
            org=_OrgStub(slug=slug),
            user=_UserStub(role=_UserRole.ADMIN, email="admin@example.com"),
        )
        assert can_create_api_keys(auth) is True


def test_api_key_auth_cannot_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="customer-org"),
        user=_UserStub(role=_UserRole.ADMIN, email="admin@example.com"),
        method=_AuthMethod.API_KEY,
    )

    assert can_create_api_keys(auth) is False


def test_cached_admin_can_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=None,
        org_slug="customer-org",
        user=None,
        user_role=_UserRole.ADMIN,
        user_email="admin@example.com",
    )

    assert can_create_api_keys(auth) is True
