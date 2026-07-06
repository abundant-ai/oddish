"""DELETE /users/me — self-serve account deletion.

The critical contract: local rows are tombstoned first, then the Clerk user is
deleted; a Clerk failure restores the rows so the flow is always retryable and
never leaves a Clerk-deleted user active locally. API-key auth must be
rejected so a key cannot destroy the account that minted it, and a sole admin
of a shared org must promote a replacement before self-deleting.
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
async def test_delete_account_tombstones_survive_transfer_failure(
    monkeypatch, user_in_two_orgs
):
    """Once the Clerk user is deleted, local tombstoning must not be rolled
    back by a tag-ownership transfer failure — that would strand a
    Clerk-deleted user as active locally. The transfer is best-effort; the
    hourly orphaned-owner sweep is the safety net."""
    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def fake_delete_clerk_user(cid: str) -> None:
        return None

    async def failing_transfer(session, *, org_id: str, deactivated_user_id: str):
        raise RuntimeError("transfer blew up")

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", fake_delete_clerk_user)
    monkeypatch.setattr(
        orgs_router, "transfer_tag_ownership_to_admin", failing_transfer
    )

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 200

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
async def test_delete_account_clerk_failure_restores_rows(
    monkeypatch, user_in_two_orgs
):
    """If Clerk deletion fails and the user is confirmed still alive in
    Clerk, the pre-committed tombstones are rolled back so the user keeps a
    working account and can retry."""
    from fastapi import HTTPException

    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def failing_delete(cid: str) -> None:
        raise HTTPException(status_code=503, detail="Clerk unreachable")

    async def clerk_alive(cid: str) -> bool | None:
        return True

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", failing_delete)
    monkeypatch.setattr(orgs_router, "_clerk_user_exists", clerk_alive)

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


async def _insert_duplicate_row(clerk_user_id: str, org_id: str) -> str:
    """Simulate a concurrent JIT provisioning reviving the identity."""
    dup_id = f"user_dup_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            UserModel(
                id=dup_id,
                org_id=org_id,
                email=f"{dup_id}@example.com",
                clerk_user_id=clerk_user_id,
                role="member",
                is_active=True,
            )
        )
    return dup_id


@requires_db
@pytest.mark.asyncio
async def test_delete_account_sweeps_rows_revived_during_clerk_call(
    monkeypatch, user_in_two_orgs
):
    """A row JIT-provisioned between the tombstone commit and Clerk deletion
    must not survive as an active orphan."""
    clerk_user_id, user_a, user_b = user_in_two_orgs
    dup_holder: dict[str, str] = {}

    async def delete_and_revive(cid: str) -> None:
        # While the "Clerk call" is in flight, a concurrent request revives
        # the identity with a fresh row.
        dup_holder["id"] = await _insert_duplicate_row(cid, user_a.org_id)

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", delete_and_revive)
    monkeypatch.setattr(orgs_router, "transfer_tag_ownership_to_admin", _noop_transfer)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.delete("/users/me")

        assert resp.status_code == 200

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        UserModel.__table__.select().where(
                            UserModel.clerk_user_id == clerk_user_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 3
        for row in rows:
            assert row["is_active"] is False
            assert row["deleted_at"] is not None
    finally:
        if "id" in dup_holder:
            async with get_session() as session:
                await session.execute(
                    UserModel.__table__.delete().where(
                        UserModel.id == dup_holder["id"]
                    )
                )


@requires_db
@pytest.mark.asyncio
async def test_delete_account_clerk_failure_restore_tombstones_revived_rows(
    monkeypatch, user_in_two_orgs
):
    """On Clerk failure the originals are restored and any row revived during
    the window is tombstoned, so one identity never has duplicate active rows."""
    from fastapi import HTTPException

    clerk_user_id, user_a, user_b = user_in_two_orgs
    dup_holder: dict[str, str] = {}

    async def revive_then_fail(cid: str) -> None:
        dup_holder["id"] = await _insert_duplicate_row(cid, user_a.org_id)
        raise HTTPException(status_code=503, detail="Clerk unreachable")

    async def clerk_alive(cid: str) -> bool | None:
        return True

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", revive_then_fail)
    monkeypatch.setattr(orgs_router, "_clerk_user_exists", clerk_alive)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.delete("/users/me")

        assert resp.status_code == 503

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        UserModel.__table__.select().where(
                            UserModel.clerk_user_id == clerk_user_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        by_id = {row["id"]: row for row in rows}
        assert by_id[user_a.id]["is_active"] is True
        assert by_id[user_a.id]["deleted_at"] is None
        assert by_id[user_b.id]["is_active"] is True
        assert by_id[user_b.id]["deleted_at"] is None
        dup = by_id[dup_holder["id"]]
        assert dup["is_active"] is False
        assert dup["deleted_at"] is not None
    finally:
        if "id" in dup_holder:
            async with get_session() as session:
                await session.execute(
                    UserModel.__table__.delete().where(
                        UserModel.id == dup_holder["id"]
                    )
                )


@requires_db
@pytest.mark.asyncio
async def test_delete_account_clerk_error_but_user_gone_treated_as_success(
    monkeypatch, user_in_two_orgs
):
    """A Clerk delete that errors (e.g. timeout after the delete landed, or a
    parallel deletion winning) must NOT restore local rows when the Clerk
    user is confirmed gone — that would resurrect an account with no sign-in."""
    from fastapi import HTTPException

    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def failing_delete(cid: str) -> None:
        raise HTTPException(status_code=503, detail="Clerk unreachable")

    async def clerk_gone(cid: str) -> bool | None:
        return False

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", failing_delete)
    monkeypatch.setattr(orgs_router, "_clerk_user_exists", clerk_gone)
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
    for row in rows:
        assert row["is_active"] is False
        assert row["deleted_at"] is not None


@requires_db
@pytest.mark.asyncio
async def test_delete_account_clerk_unknown_state_restores_rows(
    monkeypatch, user_in_two_orgs
):
    """When neither the delete nor the existence probe reaches Clerk (outage,
    misconfigured secret), the tombstones must be RESTORED and the real error
    surfaced. Keeping them would brick the account: locally deactivated while
    the Clerk sign-in still works. The false-negative direction (deletion
    actually landed) is recoverable — the sign-in is gone and the
    ``user.deleted`` webhook re-tombstones."""
    from fastapi import HTTPException

    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def failing_delete(cid: str) -> None:
        raise HTTPException(status_code=503, detail="Failed to reach Clerk: boom")

    async def clerk_unknown(cid: str) -> bool | None:
        return None

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", failing_delete)
    monkeypatch.setattr(orgs_router, "_clerk_user_exists", clerk_unknown)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 503
    # The underlying Clerk error is surfaced, not a generic retry message.
    assert "Clerk" in resp.json()["detail"]

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
    for row in rows:
        assert row["is_active"] is True
        assert row["deleted_at"] is None


@requires_db
@pytest.mark.asyncio
async def test_delete_account_post_clerk_sweep_failure_still_succeeds(
    monkeypatch, user_in_two_orgs
):
    """After Clerk deletion, a failing revived-row sweep must not turn the
    response into an error — the deletion already happened."""
    clerk_user_id, user_a, user_b = user_in_two_orgs

    async def ok_delete(cid: str) -> None:
        return None

    real_get_session = orgs_router.get_session
    calls = {"n": 0}

    def flaky_get_session():
        calls["n"] += 1
        # Session 1: initial lookup + tombstone commit. Session 2: the
        # post-Clerk revived-row sweep — make it blow up.
        if calls["n"] == 2:
            raise RuntimeError("db blip during sweep")
        return real_get_session()

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", ok_delete)
    monkeypatch.setattr(orgs_router, "transfer_tag_ownership_to_admin", _noop_transfer)
    monkeypatch.setattr(orgs_router, "get_session", flaky_get_session)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=user_a.org_id, user_id=user_a.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "clerk_user_id": clerk_user_id}


@pytest_asyncio.fixture
async def sole_admin_of_shared_org():
    """An admin whose org has one other active member and no other admin."""
    suffix = uuid.uuid4().hex[:8]
    clerk_user_id = f"clerk_adm_{suffix}"
    org = OrganizationModel(
        id=f"org_c_{suffix}", name=f"org_c_{suffix}", slug=f"org-c-{suffix}"
    )
    admin = UserModel(
        id=f"admin_{suffix}",
        org_id=org.id,
        email=f"admin_{suffix}@example.com",
        clerk_user_id=clerk_user_id,
        role="admin",
        is_active=True,
    )
    member = UserModel(
        id=f"member_{suffix}",
        org_id=org.id,
        email=f"member_{suffix}@example.com",
        clerk_user_id=f"clerk_other_{suffix}",
        role="member",
        is_active=True,
    )
    async with get_session() as session:
        session.add(org)
        await session.flush()
        session.add_all([admin, member])

    try:
        yield clerk_user_id, admin, member
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.id.in_([admin.id, member.id])
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org.id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_delete_account_blocks_sole_admin_of_shared_org(
    monkeypatch, sole_admin_of_shared_org
):
    """A sole admin of an org with other members must promote a replacement
    first; the Clerk user must not be deleted."""
    clerk_user_id, admin, member = sole_admin_of_shared_org

    deleted_clerk_ids: list[str] = []

    async def fake_delete_clerk_user(cid: str) -> None:
        deleted_clerk_ids.append(cid)

    monkeypatch.setattr(orgs_router, "_delete_clerk_user", fake_delete_clerk_user)

    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=admin.org_id, user_id=admin.id
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/users/me")

    assert resp.status_code == 400
    assert "last admin" in resp.json()["detail"]
    assert deleted_clerk_ids == []

    async with get_session() as session:
        row = (
            (
                await session.execute(
                    UserModel.__table__.select().where(UserModel.id == admin.id)
                )
            )
            .mappings()
            .one()
        )
    assert row["is_active"] is True
    assert row["deleted_at"] is None


def test_invalidate_cached_clerk_auth_drops_all_org_variants():
    from auth.verification import (
        CachedAuthData,
        get_cached_auth,
        invalidate_cached_clerk_auth,
        set_cached_auth,
    )
    from models import APIKeyScope

    def _data() -> CachedAuthData:
        return CachedAuthData(
            method=AuthMethod.CLERK_JWT,
            org_id="org_1",
            org_slug="org-1",
            user_id="user_1",
            user_email="u@example.com",
            user_role=None,
            scope=APIKeyScope.FULL,
        )

    set_cached_auth("clerk:user_del:org_1", _data())
    set_cached_auth("clerk:user_del:no-org", _data())
    set_cached_auth("clerk:user_keep:org_1", _data())

    assert invalidate_cached_clerk_auth("user_del") == 2
    assert get_cached_auth("clerk:user_del:org_1") is None
    assert get_cached_auth("clerk:user_del:no-org") is None
    assert get_cached_auth("clerk:user_keep:org_1") is not None


@requires_db
@pytest.mark.asyncio
async def test_user_deleted_webhook_tombstones_all_rows(
    monkeypatch, user_in_two_orgs
):
    """The Clerk ``user.deleted`` webhook is the cross-container safety net:
    it tombstones every active row for the deleted Clerk user."""
    from api.routers import clerk_webhooks

    clerk_user_id, user_a, user_b = user_in_two_orgs

    def fake_verify(payload: bytes, headers: dict) -> dict:
        return {"type": "user.deleted", "data": {"id": clerk_user_id}}

    monkeypatch.setattr(clerk_webhooks, "_verify_clerk_webhook", fake_verify)

    app = FastAPI()
    app.include_router(clerk_webhooks.router)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/webhooks/clerk", json={})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

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
