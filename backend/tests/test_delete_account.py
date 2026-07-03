"""DELETE /users/me — self-serve account deletion.

The critical contract: the Clerk user is deleted first (authoritative identity
store), then every local user row for that Clerk user is soft-deleted. API-key
auth must be rejected so a key cannot destroy the account that minted it.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from api.routers import orgs as orgs_router
from auth import AuthContext, AuthMethod, require_auth
from models import OrganizationModel, UserModel
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(orgs_router.router)
    return app


async def _noop_transfer(session, *, org_id: str, deactivated_user_id: str) -> int:
    return 0


@pytest.mark.asyncio
async def test_delete_account_rejects_api_key_auth():
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id="org_1", user_id="user_1"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")
    assert resp.status_code == 403


@pytest_asyncio.fixture
async def user_in_two_orgs():
    """One Clerk identity with active user rows in two orgs."""
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_del_{suffix}"
    org_a = OrganizationModel(
        id=f"org_a_{suffix}", name=f"org_a_{suffix}", slug=f"org-a-{suffix}"
    )
    org_b = OrganizationModel(
        id=f"org_b_{suffix}", name=f"org_b_{suffix}", slug=f"org-b-{suffix}"
    )
    user_a = UserModel(
        id=f"user_a_{suffix}",
        org_id=org_a.id,
        email=f"a_{suffix}@example.com",
        clerk_user_id=clerk_user_id,
        role="member",
        is_active=True,
    )
    user_b = UserModel(
        id=f"user_b_{suffix}",
        org_id=org_b.id,
        email=f"b_{suffix}@example.com",
        clerk_user_id=clerk_user_id,
        role="member",
        is_active=True,
    )
    async with get_session() as session:
        session.add_all([org_a, org_b])
        await session.flush()
        session.add_all([user_a, user_b])

    try:
        yield clerk_user_id, user_a, user_b
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([user_a.id, user_b.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_([org_a.id, org_b.id])
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_delete_account_deletes_clerk_then_tombstones_all_rows(
    monkeypatch, user_in_two_orgs
):
    clerk_user_id, user_a, user_b = user_in_two_orgs

    deleted_clerk_ids: list[str] = []

    async def fake_delete_clerk_user(cid: str) -> None:
        deleted_clerk_ids.append(cid)

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", fake_delete_clerk_user)
    # Pre-existing shared helper, not under test here: its raw SQL compares
    # role = 'OWNER', a label the userrole enum no longer carries on schemas
    # that ran the drop_owner_userrole migration, so it errors locally.
    monkeypatch.setattr(orgs_router, "transfer_tag_ownership_to_admin", _noop_transfer)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "clerk_user_id": clerk_user_id}
    assert deleted_clerk_ids == [clerk_user_id]

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    UserModel.__table__.select()
                    .where(UserModel.id.in_([user_a.id, user_b.id]))
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    for row in rows:
        assert row["is_active"] is False
        assert row["deleted_at"] is not None


@requires_db
@pytest.mark.asyncio
async def test_delete_account_clerk_failure_leaves_rows_untouched(
    monkeypatch, user_in_two_orgs
):
    """If Clerk deletion fails, no local row is tombstoned."""
    from fastapi import HTTPException

    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def failing_delete(cid: str) -> None:
        raise HTTPException(status_code=503, detail="Clerk unreachable")

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", failing_delete)
    monkeypatch.setattr(orgs_router, "transfer_tag_ownership_to_admin", _noop_transfer)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 503

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    UserModel.__table__.select()
                    .where(UserModel.id.in_([user_a.id, user_b.id]))
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    for row in rows:
        assert row["is_active"] is True
        assert row["deleted_at"] is None
