"""Billing identity must belong to the request org.

``resolve_billed_user_id`` previously checked liveness only. A payer row from
another org would silently miss its ``(org_id, user_id)`` quota override and
fall back to the default — and this org's admin list would never show it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api.routers.task_submission import resolve_billed_user_id
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserModel, UserRole


def _user(
    *,
    id: str,
    org_id: str,
    is_active: bool = True,
    deleted_at: datetime | None = None,
) -> UserModel:
    user = UserModel(
        id=id,
        org_id=org_id,
        email=f"{id}@example.com",
        github_username=None,
        clerk_user_id=f"clerk_{id}",
        role="member",
        is_active=is_active,
    )
    user.deleted_at = deleted_at
    return user


class _GetSession:
    """Minimal AsyncSession stand-in: ``get(UserModel, id)`` from a canned map."""

    def __init__(self, users: dict[str, UserModel]) -> None:
        self._users = users

    async def get(self, model, pk):  # noqa: ANN001
        if model is UserModel:
            return self._users.get(pk)
        return None


def _api_key_auth(*, org_id: str, creator_id: str) -> AuthContext:
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id=org_id,
        user_id=creator_id,
        api_key=SimpleNamespace(created_by_user_id=creator_id),  # type: ignore[arg-type]
        api_key_id="key_1",
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.TASKS,
    )


def _submission():
    return SimpleNamespace(github_id=None, github_username=None)


@pytest.mark.asyncio
async def test_payer_in_request_org_is_billed():
    payer = _user(id="payer_ok", org_id="org_1")
    session = _GetSession({payer.id: payer})
    auth = _api_key_auth(org_id="org_1", creator_id=payer.id)

    assert await resolve_billed_user_id(session, _submission(), auth) == payer.id


@pytest.mark.asyncio
async def test_payer_in_other_org_is_none_and_warns(caplog):
    payer = _user(id="payer_other", org_id="org_other")
    session = _GetSession({payer.id: payer})
    auth = _api_key_auth(org_id="org_1", creator_id=payer.id)

    with caplog.at_level(logging.WARNING, logger="api.routers.task_submission"):
        result = await resolve_billed_user_id(session, _submission(), auth)

    assert result is None
    mismatches = [r for r in caplog.records if "payer org mismatch" in r.getMessage()]
    assert mismatches, "expected a payer org-mismatch warning"
    rendered = mismatches[0].getMessage()
    assert "user_id=payer_other" in rendered
    assert "user_org_id=org_other" in rendered
    assert "request_org_id=org_1" in rendered


@pytest.mark.asyncio
async def test_soft_deleted_payer_in_request_org_is_none():
    payer = _user(
        id="payer_deleted",
        org_id="org_1",
        deleted_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    session = _GetSession({payer.id: payer})
    auth = _api_key_auth(org_id="org_1", creator_id=payer.id)

    assert await resolve_billed_user_id(session, _submission(), auth) is None


@pytest.mark.asyncio
async def test_inactive_payer_is_none():
    payer = _user(id="payer_inactive", org_id="org_1", is_active=False)
    session = _GetSession({payer.id: payer})
    auth = _api_key_auth(org_id="org_1", creator_id=payer.id)

    assert await resolve_billed_user_id(session, _submission(), auth) is None
