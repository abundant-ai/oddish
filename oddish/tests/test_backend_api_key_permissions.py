from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class _UserRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"


@dataclass
class _UserStub:
    role: _UserRole
    email: str | None


@dataclass
class _OrgStub:
    slug: str | None


@dataclass
class _AuthStub:
    org: _OrgStub | None = None
    org_slug: str | None = None
    user: _UserStub | None = None
    user_email: str | None = None
    user_role: _UserRole | None = None


def _load_permissions() -> dict[str, Any]:
    permissions_path = (
        Path(__file__).resolve().parents[2] / "backend" / "auth" / "permissions.py"
    )
    source = permissions_path.read_text(encoding="utf-8").replace(
        "from auth.types import AuthContext\nfrom models import UserRole\n",
        "",
    )
    namespace: dict[str, Any] = {"AuthContext": _AuthStub, "UserRole": _UserRole}
    exec(source, namespace)
    return namespace


def test_abundant_admin_can_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="abundant"),
        user=_UserStub(role=_UserRole.ADMIN, email="Admin@Abundant.AI"),
    )

    assert can_create_api_keys(auth) is True


def test_non_abundant_admin_cannot_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="abundant"),
        user=_UserStub(role=_UserRole.ADMIN, email="admin@example.com"),
    )

    assert can_create_api_keys(auth) is False


def test_abundant_admin_cannot_create_api_keys_in_other_org() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="customer-org"),
        user=_UserStub(role=_UserRole.ADMIN, email="admin@abundant.ai"),
    )

    assert can_create_api_keys(auth) is False


def test_member_cannot_create_api_keys_even_with_abundant_email() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=_OrgStub(slug="abundant"),
        user=_UserStub(role=_UserRole.MEMBER, email="member@abundant.ai"),
    )

    assert can_create_api_keys(auth) is False


def test_cached_abundant_admin_can_create_api_keys() -> None:
    can_create_api_keys = _load_permissions()["can_create_api_keys"]

    auth = _AuthStub(
        org=None,
        org_slug="abundant",
        user=None,
        user_role=_UserRole.ADMIN,
        user_email="admin@abundant.ai",
    )

    assert can_create_api_keys(auth) is True
