"""The billed payer must be ACTIVE or None.

``resolve_billed_user_id`` may never return a tombstoned/offboarded user id: it
would be invisible to the active-scoped admin quota list and un-overridable.
Provenance (``resolve_created_by_user_id``) is the opposite — it keeps the real
author even once offboarded — so these tests pin that the active-only guard
lives on the billing helper, not the provenance one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import api.routers.task_submission as task_submission
from api.routers.task_submission import (
    resolve_billed_user_id,
    resolve_created_by_user_id,
)
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserModel, UserRole


def _user(*, id: str, is_active: bool) -> UserModel:
    return UserModel(
        id=id,
        org_id="org_1",
        email=f"{id}@example.com",
        github_username=None,
        clerk_user_id=f"clerk_{id}",
        role="member",
        is_active=is_active,
    )


class _GetSession:
    """Minimal AsyncSession stand-in: ``get(UserModel, id)`` from a canned map.

    The resolvers under test only touch the DB through ``_active_user_id``'s
    ``session.get``; the WHERE-clause path (``resolve_connected_user``) is
    monkeypatched where a test exercises it.
    """

    def __init__(self, users: dict[str, UserModel]) -> None:
        self._users = users

    async def get(self, model, pk):  # noqa: ANN001
        if model is UserModel:
            return self._users.get(pk)
        return None


def _api_key_auth(creator_id: str) -> AuthContext:
    # For API-key auth, auth.user_id resolves to the key's creator id (which may
    # now be offboarded). api_key is duck-typed to avoid ORM construction.
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id=creator_id,
        api_key=SimpleNamespace(created_by_user_id=creator_id),  # type: ignore[arg-type]
        api_key_id="key_1",
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.TASKS,
    )


def _submission(*, github_id=None, github_username=None):
    return SimpleNamespace(github_id=github_id, github_username=github_username)


@pytest.mark.asyncio
async def test_billed_none_for_offboarded_api_key_creator():
    creator = _user(id="creator_off", is_active=False)
    session = _GetSession({creator.id: creator})
    auth = _api_key_auth(creator.id)
    submission = _submission()

    # Billing refuses the tombstoned creator ...
    assert await resolve_billed_user_id(session, submission, auth) is None
    # ... but provenance still reports the real (offboarded) author. This
    # assertion fails if the active guard is mistakenly put on created_by.
    assert await resolve_created_by_user_id(session, submission, auth) == creator.id


@pytest.mark.asyncio
async def test_billed_bills_active_api_key_creator():
    creator = _user(id="creator_on", is_active=True)
    session = _GetSession({creator.id: creator})
    auth = _api_key_auth(creator.id)

    assert (
        await resolve_billed_user_id(session, _submission(), auth) == creator.id
    )


@pytest.mark.asyncio
async def test_billed_uses_linked_github_even_when_creator_offboarded(monkeypatch):
    creator = _user(id="creator_off", is_active=False)
    author = _user(id="gh_author", is_active=True)
    session = _GetSession({creator.id: creator, author.id: author})

    async def fake_resolve_connected(session, *, org_id, github_id, github_username):
        return author

    monkeypatch.setattr(
        task_submission, "resolve_connected_user", fake_resolve_connected
    )
    auth = _api_key_auth(creator.id)
    submission = _submission(github_id="12345")

    assert await resolve_billed_user_id(session, submission, auth) == author.id


@pytest.mark.asyncio
async def test_billed_rejects_inactive_cached_owner():
    # tasks.py passes a cached owner_user_id into resolve_billed_user_id; the
    # active guard must cover that short-circuit too, not only the fallback.
    stale = _user(id="stale_owner", is_active=False)
    session = _GetSession({stale.id: stale})
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT, org_id="org_1", user_id="someone_else"
    )

    result = await resolve_billed_user_id(
        session, _submission(), auth, owner_user_id=stale.id
    )
    assert result is None
