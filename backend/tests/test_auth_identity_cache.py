"""Clerk identities stay cached for ``AUTH_IDENTITY_TTL``; role and email
come from the token on every hit; API-key entries keep the short TTL."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import auth as auth_module
from auth import get_auth_context
from auth.types import AuthMethod
from auth.verification import (
    AUTH_CACHE_TTL,
    AUTH_IDENTITY_TTL,
    CachedAuthData,
    invalidate_cached_clerk_auth,
    set_cached_auth,
)
from models import APIKeyScope, UserRole
from oddish.cache import TTLCache
import auth.verification as verification


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cache(monkeypatch):
    clock = _Clock()
    fresh: TTLCache[str, CachedAuthData] = TTLCache(AUTH_CACHE_TTL, clock=clock)
    monkeypatch.setattr(verification, "_auth_cache", fresh)
    return fresh, clock


def _claims(**overrides):
    claims = {
        "sub": "user_1",
        "org_id": "org_1",
        "email": "fresh@example.com",
        "org_role": "org:member",
    }
    claims.update(overrides)
    return claims


@pytest.mark.asyncio
async def test_hit_takes_role_and_email_from_the_verified_token(cache, monkeypatch):
    fresh, _ = cache
    set_cached_auth(
        "clerk:user_1:org_1",
        CachedAuthData(
            method=AuthMethod.CLERK_JWT,
            org_id="o-1",
            org_slug="acme",
            user_id="u-1",
            user_email="stale@example.com",
            user_role=UserRole.MEMBER,
        ),
        ttl_seconds=AUTH_IDENTITY_TTL,
    )
    monkeypatch.setattr(
        auth_module,
        "verify_clerk_jwt",
        AsyncMock(return_value=_claims(org_role="org:admin")),
    )
    forbidden_session = AsyncMock(
        side_effect=AssertionError("cache hit must not open a session")
    )
    monkeypatch.setattr(auth_module, "get_session", forbidden_session)

    context = await get_auth_context(None, authorization="Bearer a.b.c")

    assert context.user_id == "u-1"
    assert context.org_id == "o-1"
    assert context.user_role == UserRole.ADMIN  # promoted in Clerk since caching
    assert context.user_email == "fresh@example.com"
    assert context.scope == APIKeyScope.FULL
    forbidden_session.assert_not_called()


@pytest.mark.asyncio
async def test_hit_without_role_claim_keeps_the_cached_role(cache, monkeypatch):
    set_cached_auth(
        "clerk:user_1:no-org",
        CachedAuthData(
            method=AuthMethod.CLERK_JWT,
            org_id="o-personal",
            user_id="u-1",
            user_email="me@example.com",
            user_role=UserRole.ADMIN,
        ),
        ttl_seconds=AUTH_IDENTITY_TTL,
    )
    monkeypatch.setattr(
        auth_module,
        "verify_clerk_jwt",
        AsyncMock(return_value=_claims(org_id=None, org_role=None, email=None)),
    )

    context = await get_auth_context(None, authorization="Bearer a.b.c")

    assert context.user_role == UserRole.ADMIN
    assert context.user_email == "me@example.com"


@pytest.mark.asyncio
async def test_miss_caches_the_identity_for_the_long_ttl(cache, monkeypatch):
    fresh, clock = cache
    monkeypatch.setattr(
        auth_module, "verify_clerk_jwt", AsyncMock(return_value=_claims())
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    user = SimpleNamespace(id="u-1", email="fresh@example.com", role=UserRole.MEMBER)
    org = SimpleNamespace(id="o-1", slug="acme")
    monkeypatch.setattr(auth_module, "get_session", fake_session)
    monkeypatch.setattr(
        auth_module,
        "get_or_create_user_from_clerk",
        AsyncMock(return_value=(user, org)),
    )

    context = await get_auth_context(None, authorization="Bearer a.b.c")
    assert context.user_id == "u-1"

    _, expires_at = fresh._data["clerk:user_1:org_1"]
    assert expires_at == pytest.approx(clock.now + AUTH_IDENTITY_TTL)
    assert AUTH_IDENTITY_TTL > AUTH_CACHE_TTL

    # Still a hit long after the API-key TTL would have lapsed.
    clock.now += AUTH_CACHE_TTL * 5
    monkeypatch.setattr(
        auth_module,
        "get_or_create_user_from_clerk",
        AsyncMock(side_effect=AssertionError),
    )
    again = await get_auth_context(None, authorization="Bearer a.b.c")
    assert again.user_id == "u-1"


def test_api_key_entries_keep_the_short_ttl(cache):
    fresh, clock = cache
    set_cached_auth("apikey:h", CachedAuthData(method=AuthMethod.API_KEY, org_id="o-1"))
    _, expires_at = fresh._data["apikey:h"]
    assert expires_at == pytest.approx(clock.now + AUTH_CACHE_TTL)


def test_invalidate_clerk_auth_drops_every_org_variant(cache):
    set_cached_auth(
        "clerk:user_1:org_1", CachedAuthData(method=AuthMethod.CLERK_JWT, org_id="o")
    )
    set_cached_auth(
        "clerk:user_1:no-org", CachedAuthData(method=AuthMethod.CLERK_JWT, org_id="o")
    )
    set_cached_auth(
        "clerk:user_2:org_1", CachedAuthData(method=AuthMethod.CLERK_JWT, org_id="o")
    )

    assert invalidate_cached_clerk_auth("user_1") == 2
    assert verification.get_cached_auth("clerk:user_2:org_1") is not None
