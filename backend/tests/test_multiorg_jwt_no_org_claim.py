"""JWT without org_id must not invent a personal org for multi-org users."""

from __future__ import annotations

import logging
import os
import uuid

import pytest

import auth.provisioning as prov
from auth.provisioning import get_or_create_user_from_clerk
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


async def _noop_refresh(_user, _session=None) -> None:
    return None


def _mock_refresh(monkeypatch) -> None:
    monkeypatch.setattr(prov, "_refresh_user_github_identity", _noop_refresh)


def _mock_clerk_orgs(monkeypatch, org_ids: list[str]) -> None:
    async def _fetch(_clerk_user_id: str) -> list[str]:
        return list(org_ids)

    monkeypatch.setattr(prov, "fetch_clerk_org_ids_for_user", _fetch)


async def _purge(
    *, org_ids: list[str], user_ids: list[str], clerk_user_ids: list[str]
) -> None:
    async with get_session() as session:
        if user_ids:
            await session.execute(
                UserModel.__table__.delete()
                .where(UserModel.id.in_(user_ids))
                .execution_options(include_deleted=True)
            )
        if clerk_user_ids:
            await session.execute(
                UserModel.__table__.delete()
                .where(UserModel.clerk_user_id.in_(clerk_user_ids))
                .execution_options(include_deleted=True)
            )
            for clerk_user_id in clerk_user_ids:
                await session.execute(
                    OrganizationModel.__table__.delete().where(
                        OrganizationModel.slug == f"personal-{clerk_user_id}"
                    )
                )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_no_org_claim_zero_orgs_creates_personal_admin(monkeypatch) -> None:
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_multi_{suffix}"
    _mock_refresh(monkeypatch)
    _mock_clerk_orgs(monkeypatch, [])

    try:
        async with get_session() as session:
            result = await get_or_create_user_from_clerk(
                session,
                clerk_user_id,
                None,
                f"solo_{suffix}@example.com",
                None,
            )
        assert result is not None
        user, org = result
        assert org.slug == f"personal-{clerk_user_id}"
        assert user.role == UserRole.ADMIN
        assert user.org_id == org.id
    finally:
        await _purge(org_ids=[], user_ids=[], clerk_user_ids=[clerk_user_id])


@requires_db
@pytest.mark.asyncio
async def test_no_org_claim_one_provisioned_org_is_adopted(monkeypatch) -> None:
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_multi_{suffix}"
    clerk_org_id = f"org_clerk_{suffix}"
    org = OrganizationModel(
        id=f"org_one_{suffix}",
        name=f"org_one_{suffix}",
        slug=f"org-one-{suffix}",
        clerk_org_id=clerk_org_id,
    )
    _mock_refresh(monkeypatch)
    _mock_clerk_orgs(monkeypatch, [clerk_org_id])

    async with get_session() as session:
        session.add(org)

    try:
        async with get_session() as session:
            result = await get_or_create_user_from_clerk(
                session,
                clerk_user_id,
                None,
                f"one_{suffix}@example.com",
                "member",
            )
        assert result is not None
        user, adopted = result
        assert adopted.id == org.id
        assert user.org_id == org.id
        assert user.clerk_user_id == clerk_user_id
        assert adopted.slug != f"personal-{clerk_user_id}"
    finally:
        await _purge(
            org_ids=[org.id],
            user_ids=[],
            clerk_user_ids=[clerk_user_id],
        )


@requires_db
@pytest.mark.asyncio
async def test_no_org_claim_two_provisioned_orgs_returns_none(
    monkeypatch, caplog
) -> None:
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_multi_{suffix}"
    clerk_org_a = f"org_clerk_a_{suffix}"
    clerk_org_b = f"org_clerk_b_{suffix}"
    org_a = OrganizationModel(
        id=f"org_a_{suffix}",
        name=f"org_a_{suffix}",
        slug=f"org-a-{suffix}",
        clerk_org_id=clerk_org_a,
    )
    org_b = OrganizationModel(
        id=f"org_b_{suffix}",
        name=f"org_b_{suffix}",
        slug=f"org-b-{suffix}",
        clerk_org_id=clerk_org_b,
    )
    _mock_refresh(monkeypatch)
    _mock_clerk_orgs(monkeypatch, [clerk_org_a, clerk_org_b])

    async with get_session() as session:
        session.add_all([org_a, org_b])

    try:
        with caplog.at_level(logging.ERROR, logger="auth.provisioning"):
            async with get_session() as session:
                result = await get_or_create_user_from_clerk(
                    session,
                    clerk_user_id,
                    None,
                    f"multi_{suffix}@example.com",
                    None,
                )
        assert result is None
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors
        message = errors[0].getMessage()
        assert clerk_user_id in message
        assert "2" in message
        assert "org_id" in message
        assert "CLERK_JWT_TEMPLATE" in message

        async with get_session() as session:
            personal = await session.execute(
                OrganizationModel.__table__.select().where(
                    OrganizationModel.slug == f"personal-{clerk_user_id}"
                )
            )
            assert personal.first() is None
    finally:
        await _purge(
            org_ids=[org_a.id, org_b.id],
            user_ids=[],
            clerk_user_ids=[clerk_user_id],
        )


@requires_db
@pytest.mark.asyncio
async def test_no_org_claim_two_email_users_returns_none(monkeypatch, caplog) -> None:
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_multi_{suffix}"
    email = f"shared_{suffix}@example.com"
    org_a = OrganizationModel(
        id=f"org_ea_{suffix}",
        name=f"org_ea_{suffix}",
        slug=f"org-ea-{suffix}",
        clerk_org_id=f"org_clerk_ea_{suffix}",
    )
    org_b = OrganizationModel(
        id=f"org_eb_{suffix}",
        name=f"org_eb_{suffix}",
        slug=f"org-eb-{suffix}",
        clerk_org_id=f"org_clerk_eb_{suffix}",
    )
    user_a = UserModel(
        id=f"user_ea_{suffix}",
        org_id=org_a.id,
        email=email,
        clerk_user_id=f"clerk_other_a_{suffix}",
        role=UserRole.MEMBER,
        is_active=True,
    )
    user_b = UserModel(
        id=f"user_eb_{suffix}",
        org_id=org_b.id,
        email=email,
        clerk_user_id=f"clerk_other_b_{suffix}",
        role=UserRole.MEMBER,
        is_active=True,
    )
    _mock_refresh(monkeypatch)
    called = False

    async def _fetch(_clerk_user_id: str) -> list[str]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(prov, "fetch_clerk_org_ids_for_user", _fetch)

    async with get_session() as session:
        session.add_all([org_a, org_b])
        await session.flush()
        session.add_all([user_a, user_b])

    try:
        with caplog.at_level(logging.ERROR, logger="auth.provisioning"):
            async with get_session() as session:
                result = await get_or_create_user_from_clerk(
                    session,
                    clerk_user_id,
                    None,
                    email,
                    None,
                )
        assert result is None
        assert called is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors
        message = errors[0].getMessage()
        assert clerk_user_id in message
        assert "2" in message
        assert "org_id" in message
        assert "CLERK_JWT_TEMPLATE" in message

        async with get_session() as session:
            personal = await session.execute(
                OrganizationModel.__table__.select().where(
                    OrganizationModel.slug == f"personal-{clerk_user_id}"
                )
            )
            assert personal.first() is None
    finally:
        await _purge(
            org_ids=[org_a.id, org_b.id],
            user_ids=[user_a.id, user_b.id],
            clerk_user_ids=[clerk_user_id],
        )


@pytest.mark.asyncio
async def test_no_org_claim_email_in_dead_and_live_org_adopts_the_live_one(
    monkeypatch,
) -> None:
    """A membership in a DEACTIVATED org must not make the tenant look ambiguous.

    Both downstream resolvers filter ``OrganizationModel.is_active``, so a user
    with a live row in a dead org plus one real org has an unambiguous tenant.
    Counting the dead one would refuse a login that baseline allowed.
    """
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_dead_{suffix}"
    email = f"revived_{suffix}@example.com"
    dead_org = OrganizationModel(
        id=f"org_dead_{suffix}",
        name=f"org_dead_{suffix}",
        slug=f"org-dead-{suffix}",
        clerk_org_id=f"org_clerk_dead_{suffix}",
        is_active=False,
    )
    live_org = OrganizationModel(
        id=f"org_live_{suffix}",
        name=f"org_live_{suffix}",
        slug=f"org-live-{suffix}",
        clerk_org_id=f"org_clerk_live_{suffix}",
    )
    dead_user = UserModel(
        id=f"user_dead_{suffix}",
        org_id=dead_org.id,
        email=email,
        clerk_user_id=f"clerk_dead_other_{suffix}",
        role=UserRole.MEMBER,
        is_active=True,
    )
    live_user = UserModel(
        id=f"user_live_{suffix}",
        org_id=live_org.id,
        email=email,
        clerk_user_id=None,
        role=UserRole.MEMBER,
        is_active=True,
    )
    _mock_refresh(monkeypatch)

    async with get_session() as session:
        session.add_all([dead_org, live_org])
        await session.flush()
        session.add_all([dead_user, live_user])

    try:
        async with get_session() as session:
            result = await get_or_create_user_from_clerk(
                session, clerk_user_id, None, email, None
            )
            assert result is not None
            user, org = result
            assert org.id == live_org.id
            assert user.id == live_user.id
            assert user.clerk_user_id == clerk_user_id
    finally:
        await _purge(
            org_ids=[dead_org.id, live_org.id],
            user_ids=[dead_user.id, live_user.id],
            clerk_user_ids=[clerk_user_id],
        )
