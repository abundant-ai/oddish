"""1v1 quota duels: POST/GET /quotas/duels, accept, dismiss.

The winner roll is patched deterministic via orgs._pick_duel_winner; the
API-key rejection uses a dependency override with method=API_KEY (the
endpoints gate on method themselves, not a separate dependency).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api.routers.orgs as orgs_router
from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import (
    OrganizationModel,
    QuotaDuelModel,
    QuotaGambleModel,
    QuotaModel,
    UserModel,
    UserRole,
)
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


def _make_user(name: str, org_id: str, suffix: str) -> UserModel:
    return UserModel(
        id=f"user_{name}_{suffix}",
        org_id=org_id,
        email=f"{name}_{suffix}@example.com",
        github_username=name,
        clerk_user_id=f"clerk_{name}_{suffix}",
        role="member",
        is_active=True,
    )


@pytest_asyncio.fixture
async def duel_org():
    org_id = f"org_duel_{uuid.uuid4().hex[:8]}"
    suffix = uuid.uuid4().hex[:8]
    challenger = _make_user("chal", org_id, suffix)
    opponent = _make_user("oppo", org_id, suffix)
    bystander = _make_user("bystander", org_id, suffix)
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add_all([challenger, opponent, bystander])
    try:
        yield org_id, challenger, opponent, bystander
    finally:
        async with get_session() as session:
            await session.execute(
                QuotaGambleModel.__table__.delete().where(
                    QuotaGambleModel.org_id == org_id
                )
            )
            await session.execute(
                QuotaDuelModel.__table__.delete().where(
                    QuotaDuelModel.org_id == org_id
                )
            )
            await session.execute(
                QuotaModel.__table__.delete().where(QuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(UserModel.org_id == org_id)
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


async def _update_override(org_id: str, user_id: str, limit: str) -> None:
    async with get_session() as session:
        await session.execute(
            QuotaModel.__table__.update()
            .where(QuotaModel.org_id == org_id, QuotaModel.user_id == user_id)
            .values(limit_usd=Decimal(limit))
        )


async def _create_duel(client, opponent_id: str, wager: str = "10") -> dict:
    response = await client.post(
        "/quotas/duels",
        json={"opponent_user_id": opponent_id, "wager_usd": wager},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _db_duel_status(duel_id: str) -> str | None:
    async with get_session() as session:
        return await session.scalar(
            select(QuotaDuelModel.status).where(QuotaDuelModel.id == duel_id)
        )


async def _wrestle_rows(org_id: str) -> list[QuotaGambleModel]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(QuotaGambleModel).where(
                        QuotaGambleModel.org_id == org_id,
                        QuotaGambleModel.game == "wrestle",
                    )
                )
            )
            .scalars()
            .all()
        )


@requires_db
@pytest.mark.asyncio
async def test_create_and_list_visibility(duel_org):
    org_id, challenger, opponent, bystander = duel_org
    await _set_override(org_id, challenger.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
            as_challenger = (await client.get("/quotas/duels")).json()
        async with _member_client(app, org_id, opponent) as client:
            as_opponent = (await client.get("/quotas/duels")).json()
        async with _member_client(app, org_id, bystander) as client:
            as_bystander = (await client.get("/quotas/duels")).json()
    finally:
        app.dependency_overrides.clear()

    assert created["status"] == "open"
    assert created["role"] == "challenger"
    assert created["challenger"]["user_id"] == challenger.id
    assert created["opponent"]["user_id"] == opponent.id
    assert created["wager_usd"] == pytest.approx(10.0)
    assert created["winner_user_id"] is None
    assert created["i_won"] is None
    assert created["my_net_usd"] is None
    assert created["fight"] is None

    assert [d["id"] for d in as_challenger["outgoing"]] == [created["id"]]
    assert as_challenger["outgoing"][0]["role"] == "challenger"
    assert as_challenger["incoming"] == []
    assert {m["user_id"] for m in as_challenger["members"]} == {
        opponent.id,
        bystander.id,
    }

    assert [d["id"] for d in as_opponent["incoming"]] == [created["id"]]
    assert as_opponent["incoming"][0]["role"] == "opponent"
    assert as_opponent["outgoing"] == []
    assert {m["user_id"] for m in as_opponent["members"]} == {
        challenger.id,
        bystander.id,
    }

    assert as_bystander["incoming"] == []
    assert as_bystander["outgoing"] == []
    assert as_bystander["recent"] == []
    assert {m["user_id"] for m in as_bystander["members"]} == {
        challenger.id,
        opponent.id,
    }


@requires_db
@pytest.mark.asyncio
async def test_accept_settles_both_sides(monkeypatch, duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    await _set_override(org_id, opponent.id, "50")
    monkeypatch.setattr(orgs_router, "_pick_duel_winner", lambda c, o: o)
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
        async with _member_client(app, org_id, opponent) as client:
            accepted = await client.post(f"/quotas/duels/{created['id']}/accept")
            assert accepted.status_code == 200, accepted.text
            opponent_me = (await client.get("/quotas/me")).json()
            as_opponent = (await client.get("/quotas/duels")).json()
        async with _member_client(app, org_id, challenger) as client:
            challenger_me = (await client.get("/quotas/me")).json()
            as_challenger = (await client.get("/quotas/duels")).json()
    finally:
        app.dependency_overrides.clear()

    body = accepted.json()
    assert body["duel"]["status"] == "settled"
    assert body["duel"]["winner_user_id"] == opponent.id
    assert body["duel"]["i_won"] is True
    assert body["duel"]["my_net_usd"] == pytest.approx(10.0)
    assert body["duel"]["fight"]["winner_role"] == "opponent"
    assert body["limit_usd"] == pytest.approx(60.0)

    rows = await _wrestle_rows(org_id)
    assert len(rows) == 2
    by_user = {row.user_id: row for row in rows}
    assert by_user[opponent.id].net_usd == Decimal("10")
    assert by_user[opponent.id].multiplier == Decimal("2")
    assert by_user[opponent.id].won is True
    assert by_user[challenger.id].net_usd == Decimal("-10")
    assert by_user[challenger.id].multiplier == Decimal("0")
    assert by_user[challenger.id].won is False
    assert all(row.wager_usd == Decimal("10") for row in rows)
    assert all(row.detail["duel_id"] == created["id"] for row in rows)

    assert opponent_me["limit_usd"] == pytest.approx(60.0)
    assert challenger_me["limit_usd"] == pytest.approx(40.0)

    assert as_opponent["incoming"] == []
    assert [d["id"] for d in as_opponent["recent"]] == [created["id"]]
    assert as_opponent["recent"][0]["i_won"] is True
    assert as_opponent["recent"][0]["my_net_usd"] == pytest.approx(10.0)
    assert as_challenger["outgoing"] == []
    assert [d["id"] for d in as_challenger["recent"]] == [created["id"]]
    assert as_challenger["recent"][0]["i_won"] is False
    assert as_challenger["recent"][0]["my_net_usd"] == pytest.approx(-10.0)


@requires_db
@pytest.mark.asyncio
async def test_accept_by_bystander_404(duel_org):
    org_id, challenger, opponent, bystander = duel_org
    await _set_override(org_id, challenger.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
        async with _member_client(app, org_id, bystander) as client:
            response = await client.post(f"/quotas/duels/{created['id']}/accept")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404
    assert await _db_duel_status(created["id"]) == "open"


@requires_db
@pytest.mark.asyncio
async def test_accept_settled_duel_409(monkeypatch, duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    await _set_override(org_id, opponent.id, "50")
    monkeypatch.setattr(orgs_router, "_pick_duel_winner", lambda c, o: o)
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
        async with _member_client(app, org_id, opponent) as client:
            first = await client.post(f"/quotas/duels/{created['id']}/accept")
            second = await client.post(f"/quotas/duels/{created['id']}/accept")
    finally:
        app.dependency_overrides.clear()
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(await _wrestle_rows(org_id)) == 2


@requires_db
@pytest.mark.asyncio
async def test_accept_when_challenger_cannot_cover_409_rolls_back(duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    await _set_override(org_id, opponent.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "40")
        await _update_override(org_id, challenger.id, "10")
        async with _member_client(app, org_id, opponent) as client:
            response = await client.post(f"/quotas/duels/{created['id']}/accept")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert "challenger" in response.json()["detail"]
    assert await _db_duel_status(created["id"]) == "open"
    assert await _wrestle_rows(org_id) == []


@requires_db
@pytest.mark.asyncio
async def test_accept_when_opponent_cannot_cover_400(duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    await _set_override(org_id, opponent.id, "10")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "40")
        async with _member_client(app, org_id, opponent) as client:
            response = await client.post(f"/quotas/duels/{created['id']}/accept")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert await _db_duel_status(created["id"]) == "open"
    assert await _wrestle_rows(org_id) == []


@requires_db
@pytest.mark.asyncio
async def test_create_self_challenge_400(duel_org):
    org_id, challenger, _, _ = duel_org
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            response = await client.post(
                "/quotas/duels",
                json={"opponent_user_id": challenger.id, "wager_usd": "1"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400


@requires_db
@pytest.mark.asyncio
async def test_create_cross_org_opponent_404(duel_org):
    org_id, challenger, _, _ = duel_org
    other_org_id = f"org_duel_{uuid.uuid4().hex[:8]}"
    outsider = _make_user("outsider", other_org_id, uuid.uuid4().hex[:8])
    async with get_session() as session:
        session.add(OrganizationModel(id=other_org_id, name=other_org_id, slug=other_org_id))
        await session.flush()
        session.add(outsider)
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            response = await client.post(
                "/quotas/duels",
                json={"opponent_user_id": outsider.id, "wager_usd": "1"},
            )
    finally:
        app.dependency_overrides.clear()
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.org_id == other_org_id
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == other_org_id
                )
            )
    assert response.status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_create_wager_over_remaining_400(duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "5")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            response = await client.post(
                "/quotas/duels",
                json={"opponent_user_id": opponent.id, "wager_usd": "10"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "remaining quota" in response.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_create_open_limit_400(duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            for _i in range(orgs_router.DUEL_OPEN_LIMIT):
                await _create_duel(client, opponent.id, "1")
            over = await client.post(
                "/quotas/duels",
                json={"opponent_user_id": opponent.id, "wager_usd": "1"},
            )
    finally:
        app.dependency_overrides.clear()
    assert over.status_code == 400
    assert "open challenges" in over.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_dismiss_cancel_and_decline(duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            first = await _create_duel(client, opponent.id, "10")
            second = await _create_duel(client, opponent.id, "10")
            cancelled = await client.post(f"/quotas/duels/{first['id']}/dismiss")
        async with _member_client(app, org_id, opponent) as client:
            declined = await client.post(f"/quotas/duels/{second['id']}/dismiss")
    finally:
        app.dependency_overrides.clear()
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"
    assert await _db_duel_status(first["id"]) == "cancelled"
    assert await _db_duel_status(second["id"]) == "declined"


@requires_db
@pytest.mark.asyncio
async def test_dismiss_settled_duel_409(monkeypatch, duel_org):
    org_id, challenger, opponent, _ = duel_org
    await _set_override(org_id, challenger.id, "50")
    await _set_override(org_id, opponent.id, "50")
    monkeypatch.setattr(orgs_router, "_pick_duel_winner", lambda c, o: o)
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
        async with _member_client(app, org_id, opponent) as client:
            accepted = await client.post(f"/quotas/duels/{created['id']}/accept")
        async with _member_client(app, org_id, challenger) as client:
            dismissed = await client.post(f"/quotas/duels/{created['id']}/dismiss")
    finally:
        app.dependency_overrides.clear()
    assert accepted.status_code == 200
    assert dismissed.status_code == 409
    assert await _db_duel_status(created["id"]) == "settled"


@requires_db
@pytest.mark.asyncio
async def test_dismiss_by_bystander_404(duel_org):
    org_id, challenger, opponent, bystander = duel_org
    await _set_override(org_id, challenger.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            created = await _create_duel(client, opponent.id, "10")
        async with _member_client(app, org_id, bystander) as client:
            response = await client.post(f"/quotas/duels/{created['id']}/dismiss")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404
    assert await _db_duel_status(created["id"]) == "open"


@requires_db
@pytest.mark.asyncio
async def test_expired_duel_hidden_and_accept_409(duel_org):
    org_id, challenger, opponent, _ = duel_org
    duel = QuotaDuelModel(
        org_id=org_id,
        challenger_user_id=challenger.id,
        opponent_user_id=opponent.id,
        wager_usd=Decimal("10"),
        status="open",
        created_at=utcnow() - timedelta(hours=25),
    )
    async with get_session() as session:
        session.add(duel)
    app = create_app()
    try:
        async with _member_client(app, org_id, challenger) as client:
            as_challenger = (await client.get("/quotas/duels")).json()
        async with _member_client(app, org_id, opponent) as client:
            as_opponent = (await client.get("/quotas/duels")).json()
            accept = await client.post(f"/quotas/duels/{duel.id}/accept")
    finally:
        app.dependency_overrides.clear()
    for listing in (as_challenger, as_opponent):
        assert duel.id not in {
            d["id"]
            for bucket in ("incoming", "outgoing", "recent")
            for d in listing[bucket]
        }
    assert accept.status_code == 409
    assert "expired" in accept.json()["detail"]
    assert await _db_duel_status(duel.id) == "open"
    assert await _wrestle_rows(org_id) == []


@requires_db
@pytest.mark.asyncio
async def test_api_key_auth_rejected_on_all_routes(duel_org):
    org_id, challenger, _, _ = duel_org
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=challenger.id, method=AuthMethod.API_KEY
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            listed = await client.get("/quotas/duels")
            created = await client.post(
                "/quotas/duels",
                json={"opponent_user_id": "user_x", "wager_usd": "1"},
            )
            accepted = await client.post("/quotas/duels/duel_x/accept")
            dismissed = await client.post("/quotas/duels/duel_x/dismiss")
    finally:
        app.dependency_overrides.clear()
    assert listed.status_code == 403
    assert created.status_code == 403
    assert accepted.status_code == 403
    assert dismissed.status_code == 403
