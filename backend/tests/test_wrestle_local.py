"""Wrestle (local hot-seat) bet: POST /quotas/gamble/wrestle.

Trust-the-client double-or-nothing, no escrow/session/cap. Small crash-focused
coverage: a win/loss move quota the obvious way, plus the wager-cap and
API-key guards. Mirrors the house patterns in test_quota_gamble.py.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import (
    OrganizationModel,
    QuotaGambleModel,
    QuotaModel,
    UserModel,
    UserRole,
)
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _user_auth(
    *,
    org_id: str,
    user_id: str | None,
    role: UserRole = UserRole.MEMBER,
    method: AuthMethod = AuthMethod.CLERK_JWT,
) -> AuthContext:
    return AuthContext(method=method, org_id=org_id, user_id=user_id, user_role=role)


@pytest_asyncio.fixture
async def org_with_wrestler():
    org_id = f"org_wr_{uuid.uuid4().hex[:8]}"
    suffix = uuid.uuid4().hex[:8]
    wrestler = UserModel(
        id=f"user_wrestler_{suffix}",
        org_id=org_id,
        email=f"wrestler_{suffix}@example.com",
        github_username="wrestler",
        clerk_user_id=f"clerk_{suffix}",
        role="member",
        is_active=True,
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(wrestler)
    try:
        yield org_id, wrestler
    finally:
        async with get_session() as session:
            await session.execute(
                QuotaGambleModel.__table__.delete().where(
                    QuotaGambleModel.org_id == org_id
                )
            )
            await session.execute(
                QuotaModel.__table__.delete().where(QuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == wrestler.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


def _member_client(app, org_id, user):
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=user.id
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _set_override(org_id: str, user_id: str, limit: str) -> None:
    async with get_session() as session:
        session.add(
            QuotaModel(org_id=org_id, user_id=user_id, limit_usd=Decimal(limit))
        )


@requires_db
@pytest.mark.asyncio
async def test_win_raises_effective_limit(org_with_wrestler):
    org_id, wrestler = org_with_wrestler
    await _set_override(org_id, wrestler.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, wrestler) as client:
            response = await client.post(
                "/quotas/gamble/wrestle", json={"wager_usd": "10", "won": True}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["won"] is True
            assert body["net_usd"] == pytest.approx(10.0)
            assert body["limit_usd"] == pytest.approx(60.0)

            me = await client.get("/quotas/me")
    finally:
        app.dependency_overrides.clear()
    assert me.json()["limit_usd"] == pytest.approx(60.0)


@requires_db
@pytest.mark.asyncio
async def test_loss_lowers_limit(org_with_wrestler):
    org_id, wrestler = org_with_wrestler
    await _set_override(org_id, wrestler.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, wrestler) as client:
            response = await client.post(
                "/quotas/gamble/wrestle", json={"wager_usd": "10", "won": False}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["won"] is False
            assert body["net_usd"] == pytest.approx(-10.0)
            assert body["limit_usd"] == pytest.approx(40.0)
    finally:
        app.dependency_overrides.clear()


@requires_db
@pytest.mark.asyncio
async def test_guards_over_wager_and_api_key(org_with_wrestler):
    org_id, wrestler = org_with_wrestler
    await _set_override(org_id, wrestler.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, wrestler) as client:
            over = await client.post(
                "/quotas/gamble/wrestle", json={"wager_usd": "100", "won": True}
            )
            assert over.status_code == 400

        app.dependency_overrides[require_auth] = lambda: _user_auth(
            org_id=org_id, user_id=wrestler.id, method=AuthMethod.API_KEY
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            keyed = await client.post(
                "/quotas/gamble/wrestle", json={"wager_usd": "1", "won": True}
            )
            assert keyed.status_code == 403
    finally:
        app.dependency_overrides.clear()
