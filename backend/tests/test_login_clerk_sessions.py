"""Login must release its database session before either Clerk HTTP request."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import auth.provisioning as prov
import httpx
import pytest
from fastapi import HTTPException
from models import OrganizationModel, UserModel, UserRole


@pytest.fixture
def login(monkeypatch):
    state = {"sessions": 0, "opened": 0}

    @asynccontextmanager
    async def session():
        state["sessions"] += 1
        state["opened"] += 1
        try:
            yield AsyncMock()
        finally:
            state["sessions"] -= 1

    org = OrganizationModel(
        id="local-org", name="Example", slug="example", is_active=True
    )
    user = UserModel(
        id="local-user",
        org_id=org.id,
        clerk_user_id="clerk-user",
        email="real@example.com",
        role=UserRole.MEMBER,
        is_active=True,
    )
    monkeypatch.setattr(prov, "get_session", session)
    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "test")
    return state, org, user


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_org", [False, True])
@pytest.mark.parametrize("linked_user", [False, True])
async def test_http_has_no_session_and_writes_use_new_session(
    login, monkeypatch, existing_org, linked_user
):
    state, org, user = login
    if not linked_user:
        user.clerk_user_id = None
    lookup = AsyncMock(
        side_effect=[org if existing_org else None, org if existing_org else None]
    )
    monkeypatch.setattr(prov, "get_org_from_clerk_id", lookup)
    monkeypatch.setattr(prov, "_find_user_in_org", AsyncMock(return_value=user))
    calls = []

    def respond(request):
        assert state["sessions"] == 0
        calls.append(request.url.path)
        if "/organizations/" in request.url.path:
            return httpx.Response(200, json={"name": "Example", "slug": "example"})
        return httpx.Response(200, json={"external_accounts": []})

    monkeypatch.setattr(
        prov,
        "RequestTimedAsyncClient",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )

    async def sync(*args):
        assert state == {"sessions": 1, "opened": 2}
        assert calls == ["/v1/organizations/clerk-org", "/v1/users/clerk-user"]
        return org

    monkeypatch.setattr(prov, "sync_clerk_org", sync)

    async def update(*args, **kwargs):
        user.clerk_user_id = "clerk-user"
        return user

    upsert = AsyncMock(side_effect=update)
    monkeypatch.setattr(prov, "get_or_create_user_in_org", upsert)
    result = await prov.get_or_create_user_from_clerk(
        "clerk-user", "clerk-org", None, "member"
    )
    assert result == (user, org)
    assert user.github_id_checked_at is not None
    assert upsert.call_args.kwargs == {"refresh_github_identity": False}
    assert state["sessions"] == 0
    assert len(calls) == (1 if existing_org else 2)


@pytest.mark.asyncio
async def test_known_identity_keeps_one_session_and_updates_claims(login, monkeypatch):
    state, org, user = login
    user.github_id_checked_at = datetime.now(timezone.utc)
    user.github_username = "octocat"
    monkeypatch.setattr(prov, "get_org_from_clerk_id", AsyncMock(return_value=org))
    monkeypatch.setattr(prov, "_find_user_in_org", AsyncMock(return_value=user))
    monkeypatch.setattr(
        prov,
        "fetch_github_identity_from_clerk",
        AsyncMock(side_effect=AssertionError("unexpected HTTP")),
    )
    assert await prov.get_or_create_user_from_clerk(
        "clerk-user", "clerk-org", None, "admin"
    ) == (
        user,
        org,
    )
    assert user.email == "real@example.com"
    assert user.role == UserRole.ADMIN
    assert user.attribution_cache["github_handles"] == ["octocat"]
    assert state == {"sessions": 0, "opened": 1}


@pytest.mark.asyncio
async def test_org_deactivated_during_http_is_not_provisioned(login, monkeypatch):
    _state, org, user = login
    monkeypatch.setattr(
        prov, "get_org_from_clerk_id", AsyncMock(side_effect=[org, None])
    )
    monkeypatch.setattr(prov, "_find_user_in_org", AsyncMock(return_value=user))
    monkeypatch.setattr(
        prov, "fetch_github_identity_from_clerk", AsyncMock(return_value=None)
    )
    upsert = AsyncMock(side_effect=AssertionError("must not create user"))
    monkeypatch.setattr(prov, "get_or_create_user_in_org", upsert)
    assert (
        await prov.get_or_create_user_from_clerk("clerk-user", "clerk-org", None, None)
        is None
    )
    upsert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [(404, 403), (500, 503)])
async def test_org_fetch_failure_does_not_open_write_session(
    login, monkeypatch, status, expected
):
    state, _, _ = login
    monkeypatch.setattr(prov, "get_org_from_clerk_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        prov,
        "RequestTimedAsyncClient",
        lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(status)), **kw
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await prov.get_or_create_user_from_clerk("clerk-user", "clerk-org", None, None)
    assert exc.value.status_code == expected
    assert state == {"sessions": 0, "opened": 1}


@pytest.mark.asyncio
async def test_first_login_releases_real_postgres_connection(monkeypatch):
    import os
    import uuid

    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("local PostgreSQL required")
    from oddish.db.connection import engine

    clerk_org = f"org_{uuid.uuid4().hex}"
    clerk_user = f"user_{uuid.uuid4().hex}"
    requests = []

    def respond(request):
        assert engine.pool.checkedout() == 0
        requests.append(request.url.path)
        if "/organizations/" in request.url.path:
            return httpx.Response(200, json={"name": "Session test", "slug": clerk_org})
        return httpx.Response(
            200,
            json={
                "external_accounts": [
                    {
                        "provider": "oauth_github",
                        "username": "session-test",
                        "provider_user_id": uuid.uuid4().hex,
                    }
                ]
            },
        )

    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "test")
    monkeypatch.setattr(
        prov,
        "RequestTimedAsyncClient",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw),
    )
    user, org = await prov.get_or_create_user_from_clerk(
        clerk_user, clerk_org, "session@example.test", "admin"
    )
    assert org.execution_enabled is False
    assert user.github_id and user.github_username == "session-test"
    assert user.role == UserRole.ADMIN
    assert len(requests) == 2
    again, same_org = await prov.get_or_create_user_from_clerk(
        clerk_user, clerk_org, None, "member"
    )
    assert again.id == user.id and same_org.id == org.id
    assert again.email == "session@example.test"
    assert again.role == UserRole.MEMBER
    assert len(requests) == 2
