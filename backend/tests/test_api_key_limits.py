from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from api.routers import api_keys as api_keys_router
from api.schemas import CreateAPIKeyRequest, UpdateAPIKeyRequest
from auth.types import AuthContext, AuthMethod
from fastapi import HTTPException
from models import APIKeyScope, UserRole


def _auth(*, user_id: str = "user-1", role: UserRole = UserRole.MEMBER):
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org-1",
        user_id=user_id,
        user_role=role,
    )


def _key(*, creator: str = "user-1"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id="key-1",
        name="CI",
        key_prefix="ok_example",
        scope=APIKeyScope.TASKS,
        org_id="org-1",
        created_by_user_id=creator,
        is_active=True,
        is_internal=False,
        expires_at=None,
        last_used_at=None,
        limit_usd=None,
        created_at=now,
    )


class _Session:
    def __init__(self, key):
        self.key = key
        self.committed = False

    async def scalar(self, _stmt):
        return self.key

    async def commit(self):
        self.committed = True


def _install_session(monkeypatch, session):
    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(api_keys_router, "get_session", get_session)


def test_api_key_limit_request_validation():
    assert CreateAPIKeyRequest(name="CI", limit_usd="12.50").limit_usd == Decimal(
        "12.50"
    )
    assert UpdateAPIKeyRequest(limit_usd=None).limit_usd is None
    with pytest.raises(ValueError):
        UpdateAPIKeyRequest(limit_usd=0)


@pytest.mark.asyncio
async def test_creator_can_edit_own_api_key_limit(monkeypatch):
    key = _key()
    session = _Session(key)
    _install_session(monkeypatch, session)

    response = await api_keys_router.update_api_key(
        "key-1", UpdateAPIKeyRequest(limit_usd="7.25"), _auth()
    )

    assert key.limit_usd == Decimal("7.25")
    assert response.limit_usd == 7.25
    assert session.committed is True


@pytest.mark.asyncio
async def test_member_cannot_edit_another_users_key(monkeypatch):
    session = _Session(_key(creator="user-2"))
    _install_session(monkeypatch, session)

    with pytest.raises(HTTPException) as raised:
        await api_keys_router.update_api_key(
            "key-1", UpdateAPIKeyRequest(limit_usd="7.25"), _auth()
        )

    assert raised.value.status_code == 404
    assert session.committed is False
