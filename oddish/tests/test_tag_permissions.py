"""Tests for tag_permissions plane helpers.

We fake out the Clerk-backed UserRole by passing a small stand-in
object with ``user`` / ``user_role`` / ``method`` attributes.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _Role:
    value: str


@dataclass
class _User:
    id: str
    role: _Role | None


@dataclass
class _AuthLike:
    method: str = "clerk_jwt"
    user_id: str | None = "u-1"
    user: _User | None = None
    user_role: _Role | None = None
    scope: str = "full"
    org_id: str | None = "org-1"


def test_is_org_admin_for_admin_role():
    from oddish.core.tag_permissions import is_org_admin

    auth = _AuthLike(user=_User(id="u-1", role=_Role(value="admin")))
    assert is_org_admin(auth) is True


def test_is_org_admin_for_member_role_is_false():
    from oddish.core.tag_permissions import is_org_admin

    auth = _AuthLike(user=_User(id="u-1", role=_Role(value="member")))
    assert is_org_admin(auth) is False


def test_is_org_admin_for_api_key_returns_false():
    from oddish.core.tag_permissions import is_org_admin

    auth = _AuthLike(method="api_key", user=None, user_role=None)
    assert is_org_admin(auth) is False


def test_can_use_apply_requires_write_scope():
    from oddish.core.tag_permissions import can_use_apply

    full = _AuthLike(scope="full")
    tasks = _AuthLike(scope="tasks")
    read = _AuthLike(scope="read")

    assert can_use_apply(full, target_org_id="org-1") is True
    assert can_use_apply(tasks, target_org_id="org-1") is True
    assert can_use_apply(read, target_org_id="org-1") is False


def test_can_use_apply_rejects_cross_org():
    from oddish.core.tag_permissions import can_use_apply

    full = _AuthLike(scope="full", org_id="org-1")
    assert can_use_apply(full, target_org_id="org-2") is False


def test_can_use_apply_reserved_namespace_requires_admin():
    from oddish.core.tag_permissions import can_use_apply

    member = _AuthLike(scope="full", user=_User(id="u-1", role=_Role(value="member")))
    admin = _AuthLike(scope="full", user=_User(id="u-1", role=_Role(value="admin")))
    assert can_use_apply(member, target_org_id="org-1", reserved=True) is False
    assert can_use_apply(admin, target_org_id="org-1", reserved=True) is True


def test_can_definition_capability_admin_bypass():
    from oddish.core.tag_permissions import can_definition_capability

    class _FakeSession:
        async def execute(self, *a, **k):
            class _R:
                def scalar(self):
                    return None

                def all(self):
                    return []

            return _R()

        async def scalar(self, *a, **k):
            return None

    admin = _AuthLike(user=_User(id="u-1", role=_Role(value="admin")))
    ok = _run(
        can_definition_capability(
            _FakeSession(),
            admin,
            tag={"id": "tag-1", "owner_user_id": "other"},
            capability="RENAME",
        )
    )
    assert ok is True


def test_can_definition_capability_owner_bypass():
    from oddish.core.tag_permissions import can_definition_capability

    class _FakeSession:
        async def execute(self, *a, **k):
            class _R:
                def scalar(self):
                    return None

                def all(self):
                    return []

            return _R()

        async def scalar(self, *a, **k):
            return None

    owner = _AuthLike(user=_User(id="u-owner", role=_Role(value="member")))
    ok = _run(
        can_definition_capability(
            _FakeSession(),
            owner,
            tag={"id": "tag-1", "owner_user_id": "u-owner"},
            capability="EDIT",
        )
    )
    assert ok is True


def test_can_definition_capability_grant_via_tag_grants():
    from oddish.core.tag_permissions import can_definition_capability

    class _FakeSession:
        async def scalar(self, stmt, params=None):
            # Indicates a row in tag_grants matched.
            return 1

    user = _AuthLike(user=_User(id="u-2", role=_Role(value="member")))
    ok = _run(
        can_definition_capability(
            _FakeSession(),
            user,
            tag={"id": "tag-1", "owner_user_id": "u-other"},
            capability="MERGE",
        )
    )
    assert ok is True


def test_can_definition_capability_member_without_grant_is_false():
    from oddish.core.tag_permissions import can_definition_capability

    class _FakeSession:
        async def scalar(self, stmt, params=None):
            return None

    user = _AuthLike(user=_User(id="u-2", role=_Role(value="member")))
    ok = _run(
        can_definition_capability(
            _FakeSession(),
            user,
            tag={"id": "tag-1", "owner_user_id": "u-other"},
            capability="DELETE",
        )
    )
    assert ok is False


def test_can_manage_grants_requires_owner_or_admin():
    from oddish.core.tag_permissions import can_manage_grants

    owner = _AuthLike(user=_User(id="u-owner", role=_Role(value="member")))
    admin = _AuthLike(user=_User(id="u-x", role=_Role(value="admin")))
    member = _AuthLike(user=_User(id="u-x", role=_Role(value="member")))

    assert can_manage_grants(owner, tag={"id": "t", "owner_user_id": "u-owner"}) is True
    assert can_manage_grants(admin, tag={"id": "t", "owner_user_id": "other"}) is True
    assert can_manage_grants(member, tag={"id": "t", "owner_user_id": "other"}) is False


def test_grant_tag_capability_core_inserts_row(monkeypatch):
    from oddish.core import tags_core

    class _FakeSession:
        def __init__(self):
            self.executed = []

        async def execute(self, stmt, params=None):
            self.executed.append((str(stmt), dict(params or {})))

            class _R:
                @property
                def rowcount(self_inner):
                    return 1

            return _R()

        async def scalar(self, stmt, params=None):
            return None

        async def flush(self):
            pass

    s = _FakeSession()

    # Old result helper did not expose ``all()`` because the old code did
    # not capture ``RETURNING id``. Patch the local fake to support it.
    async def _exec(stmt, params=None):
        s.executed.append((str(stmt), dict(params or {})))

        class _R:
            def all(self_inner):
                return []

            @property
            def rowcount(self_inner):
                return 1

        return _R()

    s.execute = _exec  # type: ignore[method-assign]

    _run(
        tags_core.grant_tag_capability_core(
            s,
            tag_id="tag-1",
            org_id="org-1",
            actor_user_id="u-owner",
            principal_type="USER",
            principal_user_id="u-2",
            capability="RENAME",
        )
    )
    assert any("INSERT INTO tag_grants" in stmt for stmt, _ in s.executed)


def test_revoke_tag_capability_core_soft_deletes(monkeypatch):
    from oddish.core import tags_core

    class _FakeSession:
        def __init__(self):
            self.executed = []

        async def execute(self, stmt, params=None):
            self.executed.append((str(stmt), dict(params or {})))

            class _R:
                @property
                def rowcount(self_inner):
                    return 1

            return _R()

        async def scalar(self, stmt, params=None):
            return None

        async def flush(self):
            pass

    s = _FakeSession()
    _run(
        tags_core.revoke_tag_capability_core(
            s,
            grant_id="grant-1",
            tag_id="tag-1",
            org_id="org-1",
            actor_user_id="u-owner",
        )
    )
    assert any("UPDATE tag_grants" in stmt for stmt, _ in s.executed)
