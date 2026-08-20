"""The billed payer must be ACTIVE or None.

``resolve_sweep_attribution`` may never bill a tombstoned/offboarded user id: it
would be invisible to the active-scoped admin quota list and un-overridable.
Provenance is the opposite — it keeps the real
author even once offboarded — so these tests pin that the active-only guard
lives on the billing helper, not the provenance one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import api.routers.task_submission as task_submission
from api.routers.task_submission import (
    resolve_sweep_attribution,
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
    attribution = await resolve_sweep_attribution(session, submission, auth)
    assert attribution.billed_user_id is None
    # ... but provenance still reports the real (offboarded) author. This
    # assertion fails if the active guard is mistakenly put on created_by.
    assert attribution.task_created_by_user_id == creator.id


@pytest.mark.asyncio
async def test_billed_bills_active_api_key_creator():
    creator = _user(id="creator_on", is_active=True)
    session = _GetSession({creator.id: creator})
    auth = _api_key_auth(creator.id)

    assert (
        await resolve_sweep_attribution(session, _submission(), auth)
    ).billed_user_id == creator.id


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

    assert (
        await resolve_sweep_attribution(session, submission, auth)
    ).billed_user_id == author.id


@pytest.mark.asyncio
async def test_billed_falls_back_to_active_task_creator_when_owner_is_inactive():
    creator = _user(id="creator_on", is_active=True)
    author = _user(id="author_off", is_active=False)
    session = _GetSession({creator.id: creator, author.id: author})
    auth = _api_key_auth(creator.id)

    attribution = await resolve_sweep_attribution(
        session,
        _submission(github_id="author"),
        auth,
        connected_user=author,
    )

    assert attribution.experiment_owner_user_id == author.id
    assert attribution.task_created_by_user_id == creator.id
    assert attribution.billed_user_id == creator.id


@pytest.mark.asyncio
async def test_billed_rejects_inactive_authenticated_owner():
    stale = _user(id="stale_owner", is_active=False)
    session = _GetSession({stale.id: stale})
    auth = AuthContext(method=AuthMethod.CLERK_JWT, org_id="org_1", user_id=stale.id)

    result = await resolve_sweep_attribution(session, _submission(), auth)
    assert result.experiment_owner_user_id == stale.id
    assert result.billed_user_id is None
