"""Quota casino: POST /quotas/gamble, GET /quotas/gambles, effective-limit fold.

The flip is patched deterministic via orgs._flip_coin; the API-key rejection
uses a dependency override with method=API_KEY (the endpoint gates on method
itself, not a separate dependency).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import api.routers.orgs as orgs_router
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
from oddish.config import settings
from oddish.db import get_session, utcnow

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
async def org_with_gambler():
    org_id = f"org_gmb_{uuid.uuid4().hex[:8]}"
    suffix = uuid.uuid4().hex[:8]
    gambler = UserModel(
        id=f"user_gambler_{suffix}",
        org_id=org_id,
        email=f"gambler_{suffix}@example.com",
        github_username="gambler",
        clerk_user_id=f"clerk_{suffix}",
        role="member",
        is_active=True,
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(gambler)
    try:
        yield org_id, gambler
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
                UserModel.__table__.delete().where(UserModel.id == gambler.id)
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
async def test_win_raises_effective_limit(monkeypatch, org_with_gambler):
    org_id, gambler = org_with_gambler
    await _set_override(org_id, gambler.id, "50")
    monkeypatch.setattr(orgs_router, "_flip_coin", lambda: "heads")
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            response = await client.post(
                "/quotas/gamble", json={"wager_usd": "10", "side": "heads"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["won"] is True
            assert body["result"] == "heads"
            assert body["net_usd"] == pytest.approx(10.0)
            assert body["limit_usd"] == pytest.approx(60.0)

            me = await client.get("/quotas/me")
    finally:
        app.dependency_overrides.clear()
    assert me.json()["limit_usd"] == pytest.approx(60.0)


@requires_db
@pytest.mark.asyncio
async def test_loss_lowers_limit_and_floors_at_zero(monkeypatch, org_with_gambler):
    org_id, gambler = org_with_gambler
    await _set_override(org_id, gambler.id, "5")
    monkeypatch.setattr(orgs_router, "_flip_coin", lambda: "tails")
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            response = await client.post(
                "/quotas/gamble", json={"wager_usd": "5", "side": "heads"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["won"] is False
            assert body["net_usd"] == pytest.approx(-5.0)
            assert body["limit_usd"] == pytest.approx(0.0)

            broke = await client.post(
                "/quotas/gamble", json={"wager_usd": "1", "side": "heads"}
            )
    finally:
        app.dependency_overrides.clear()
    assert broke.status_code == 400
    assert "remaining quota" in broke.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_wager_over_remaining_rejected_without_row(org_with_gambler):
    org_id, gambler = org_with_gambler
    await _set_override(org_id, gambler.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            response = await client.post(
                "/quotas/gamble", json={"wager_usd": "100", "side": "heads"}
            )
            assert response.status_code == 400
            history = await client.get("/quotas/gambles")
    finally:
        app.dependency_overrides.clear()
    assert history.json()["items"] == []


@requires_db
@pytest.mark.asyncio
async def test_api_key_auth_rejected(org_with_gambler):
    org_id, gambler = org_with_gambler
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=gambler.id, method=AuthMethod.API_KEY
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            post_response = await client.post(
                "/quotas/gamble", json={"wager_usd": "1", "side": "heads"}
            )
            get_response = await client.get("/quotas/gambles")
    finally:
        app.dependency_overrides.clear()
    assert post_response.status_code == 403
    assert get_response.status_code == 403


@requires_db
@pytest.mark.asyncio
async def test_history_orders_newest_first_and_sums_net(monkeypatch, org_with_gambler):
    org_id, gambler = org_with_gambler
    await _set_override(org_id, gambler.id, "50")
    monkeypatch.setattr(orgs_router, "_flip_coin", lambda: "heads")
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            first = await client.post(
                "/quotas/gamble", json={"wager_usd": "10", "side": "heads"}
            )
            second = await client.post(
                "/quotas/gamble", json={"wager_usd": "5", "side": "tails"}
            )
            assert first.status_code == 200
            assert second.status_code == 200
            history = await client.get("/quotas/gambles")
    finally:
        app.dependency_overrides.clear()
    body = history.json()
    assert body["net_usd"] == pytest.approx(5.0)
    assert [item["net_usd"] for item in body["items"]] == [
        pytest.approx(-5.0),
        pytest.approx(10.0),
    ]
    assert [item["won"] for item in body["items"]] == [False, True]


@requires_db
@pytest.mark.asyncio
async def test_gambles_age_out_of_window(org_with_gambler):
    org_id, gambler = org_with_gambler
    async with get_session() as session:
        session.add(
            QuotaGambleModel(
                org_id=org_id,
                user_id=gambler.id,
                wager_usd=Decimal("100"),
                net_usd=Decimal("-100"),
                won=False,
                created_at=utcnow() - timedelta(hours=25),
            )
        )
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            me = await client.get("/quotas/me")
            history = await client.get("/quotas/gambles")
    finally:
        app.dependency_overrides.clear()
    assert me.json()["limit_usd"] == pytest.approx(
        float(settings.default_daily_quota_usd)
    )
    assert history.json()["items"] == []
    assert history.json()["net_usd"] == pytest.approx(0.0)


@requires_db
@pytest.mark.asyncio
async def test_wager_validation(org_with_gambler):
    org_id, gambler = org_with_gambler
    app = create_app()
    try:
        async with _member_client(app, org_id, gambler) as client:
            zero = await client.post(
                "/quotas/gamble", json={"wager_usd": "0", "side": "heads"}
            )
            bad_side = await client.post(
                "/quotas/gamble", json={"wager_usd": "1", "side": "edge"}
            )
    finally:
        app.dependency_overrides.clear()
    assert zero.status_code == 422
    assert bad_side.status_code == 422
