"""Push-based github_id sync via Clerk user.created and user.updated webhooks.

These exercise the sync helper behind POST /webhooks/clerk against Postgres.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from api.routers.clerk_webhooks import _sync_github_id_from_user_event
from auth.provisioning import _marker_is_fresh
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _event(clerk_user_id: str, *, github: dict | None = None) -> dict:
    external_accounts = []
    if github is not None:
        account = {
            "provider": "oauth_github",
            "username": "octocat",
            "email_address": "octo@example.com",
            "provider_user_id": "583231",
        }
        account.update(github)
        external_accounts.append(account)
    return {"id": clerk_user_id, "external_accounts": external_accounts}


async def _sync(data: dict) -> None:
    async with get_session() as session:
        await _sync_github_id_from_user_event(session, data)
        await session.commit()


async def _add_org(org_id: str) -> None:
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))


async def _add_user(org_id: str, clerk_user_id: str, **overrides) -> str:
    uid = f"user_{uuid.uuid4().hex[:8]}"
    base = {
        "id": uid,
        "org_id": org_id,
        "email": f"{uid}@example.com",
        "clerk_user_id": clerk_user_id,
        "role": UserRole.MEMBER,
        "is_active": True,
    }
    base.update(overrides)
    async with get_session() as session:
        session.add(UserModel(**base))
    return uid


async def _get(uid: str) -> UserModel:
    async with get_session() as session:
        return await session.get(UserModel, uid)


async def _purge(org_ids: list[str]) -> None:
    async with get_session() as session:
        await session.execute(
            UserModel.__table__.delete().where(UserModel.org_id.in_(org_ids))
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(
                OrganizationModel.id.in_(org_ids)
            )
        )


@requires_db
@pytest.mark.asyncio
async def test_user_updated_sets_id_and_handle_on_every_org_row() -> None:
    """A GitHub payload sets github_id and username on every active org row."""
    org_a = f"org_wh_{uuid.uuid4().hex[:8]}"
    org_b = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_a)
        await _add_org(org_b)
        a = await _add_user(org_a, clerk)
        b = await _add_user(org_b, clerk)

        await _sync(_event(clerk, github={}))

        for uid in (a, b):
            row = await _get(uid)
            assert row.github_id == "583231"
            assert row.github_username == "octocat"
    finally:
        await _purge([org_a, org_b])


@requires_db
@pytest.mark.asyncio
async def test_org_scoped_collision_skips_only_that_org() -> None:
    """An active id collision skips only the colliding org."""
    org_a = f"org_wh_{uuid.uuid4().hex[:8]}"
    org_b = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_a)
        await _add_org(org_b)
        # An unrelated active holder in org_a already owns 583231.
        await _add_user(org_a, f"clerk_{uuid.uuid4().hex[:8]}", github_id="583231")
        a = await _add_user(org_a, clerk)
        b = await _add_user(org_b, clerk)

        await _sync(_event(clerk, github={}))

        row_a = await _get(a)
        assert row_a.github_id is None  # org_a collision skipped, fail-open
        row_b = await _get(b)
        assert row_b.github_id == "583231"  # other org still claims it
    finally:
        await _purge([org_a, org_b])


@requires_db
@pytest.mark.asyncio
async def test_no_github_account_stamps_marker_and_keeps_existing_id() -> None:
    """A no-GitHub payload stamps the marker without clearing an existing id."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="preexisting", github_username="octocat"
        )

        await _sync(_event(clerk, github=None))

        row = await _get(uid)
        assert row.github_id == "preexisting"  # not cleared
        marker = (row.attribution_cache or {}).get("github_id_checked")
        assert isinstance(marker, str) and _marker_is_fresh(marker)
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_truthy_id_does_not_stamp_marker() -> None:
    """A payload with github_id claims it without stamping the checked marker."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(org_id, clerk)

        await _sync(_event(clerk, github={}))

        row = await _get(uid)
        assert row.github_id == "583231"
        assert "github_id_checked" not in (row.attribution_cache or {})
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_unknown_clerk_user_id_is_noop() -> None:
    """An unknown clerk_user_id is a no-op."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    other_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(org_id, other_clerk)

        await _sync(_event(f"clerk_{uuid.uuid4().hex[:8]}", github={}))

        row = await _get(uid)
        assert row.github_id is None
        assert row.github_username is None
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_redelivery_is_idempotent() -> None:
    """Redelivering the same event is idempotent."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(org_id, clerk)

        event = _event(clerk, github={})
        await _sync(event)
        first = await _get(uid)
        assert first.github_id == "583231"
        first_cache = dict(first.attribution_cache or {})

        await _sync(event)
        second = await _get(uid)
        assert second.github_id == "583231"
        assert second.github_username == "octocat"
        assert dict(second.attribution_cache or {}) == first_cache
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_checked_absent_user_then_linked_gets_id() -> None:
    """A checked-absent user gets github_id when a later webhook links GitHub."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id,
            clerk,
            github_username="octocat",
            attribution_cache={
                "github_id_checked": datetime.now(timezone.utc).isoformat()
            },
        )

        await _sync(_event(clerk, github={}))

        row = await _get(uid)
        assert row.github_id == "583231"
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_missing_id_in_payload_is_noop() -> None:
    """A malformed-but-well-formed-enough event with no data.id is a safe no-op."""
    await _sync({"external_accounts": []})
