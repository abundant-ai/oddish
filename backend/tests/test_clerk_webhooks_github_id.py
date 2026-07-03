"""Push-based github_id sync via Clerk user.created and user.updated webhooks.

These exercise the sync helper behind POST /webhooks/clerk against Postgres.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

import api.routers.clerk_webhooks as webhooks
import auth.provisioning as prov
from api.routers.clerk_webhooks import _sync_github_id_from_user_event
from auth.provisioning import ClerkGithubIdentity, _marker_is_fresh
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


async def _sync(data: dict, *, allow_unlink: bool = True) -> None:
    """Default models user.updated (unlink-capable); pass False for user.created."""
    async with get_session() as session:
        await _sync_github_id_from_user_event(session, data, allow_unlink=allow_unlink)
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
async def test_no_github_account_clears_stale_id_and_stamps_marker() -> None:
    """A definitive no-GitHub payload (Clerk unlinked GitHub) drops the stale id
    so the linkage gate stops trusting it, and stamps the checked marker."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="preexisting", github_username="octocat"
        )

        await _sync(_event(clerk, github=None))

        row = await _get(uid)
        assert row.github_id is None  # stale id cleared on unlink
        assert _marker_is_fresh(row.github_id_checked_at)
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_stale_user_created_event_does_not_clear_linked_id() -> None:
    """A retried/out-of-order user.created (pre-link snapshot, empty
    external_accounts) must NOT unlink an already-linked row: Svix does not
    guarantee delivery order, and user.created is by definition older than any
    linked state we hold. Positive signals still apply; the clear does not."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="linked", github_username="octocat"
        )

        await _sync(_event(clerk, github=None), allow_unlink=False)

        row = await _get(uid)
        assert row.github_id == "linked"
        assert row.github_id_checked_at is None
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_payload_without_external_accounts_does_not_clear() -> None:
    """A user.updated payload that omits the external_accounts key entirely is
    a slimmed/partial payload, not evidence of absence — it must not unlink."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="linked", github_username="octocat"
        )

        await _sync({"id": clerk})  # no external_accounts key at all

        row = await _get(uid)
        assert row.github_id == "linked"
        assert row.github_id_checked_at is None
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_relink_replaces_changed_github_id() -> None:
    """Clerk reporting a different github_id (user linked a new GitHub account)
    replaces the stale id instead of keeping the old one."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="old-id", github_username="oldcat"
        )

        await _sync(
            _event(clerk, github={"provider_user_id": "new-id", "username": "newcat"})
        )

        row = await _get(uid)
        assert row.github_id == "new-id"
        assert row.github_username == "newcat"
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_partial_answer_keeps_existing_id() -> None:
    """A partial payload (username present, provider_user_id null) is NOT a
    definitive no-github answer, so it must not clear an already-linked id."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(
            org_id, clerk, github_id="keep-me", github_username="octocat"
        )

        await _sync(_event(clerk, github={"provider_user_id": None}))

        row = await _get(uid)
        assert row.github_id == "keep-me"  # partial answer must not clear
        assert row.github_id_checked_at is None
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
        assert row.github_id_checked_at is None
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_username_no_id_does_not_stamp_marker() -> None:
    """A partial payload (github username present, provider_user_id null) sets the
    username but does NOT stamp the checked marker, so the row stays eligible for
    a later run once Clerk reports the id."""
    org_id = f"org_wh_{uuid.uuid4().hex[:8]}"
    clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    try:
        await _add_org(org_id)
        uid = await _add_user(org_id, clerk)

        await _sync(_event(clerk, github={"provider_user_id": None}))

        row = await _get(uid)
        assert row.github_id is None
        assert row.github_username == "octocat"
        assert row.github_id_checked_at is None
    finally:
        await _purge([org_id])


@requires_db
@pytest.mark.asyncio
async def test_membership_created_syncs_github_id_from_clerk(monkeypatch) -> None:
    """organizationMembership.created creates the user AND fetches the GitHub
    identity from Clerk (the payload has no external_accounts), so a new member
    lands with github_id + username like login-time JIT provisioning."""
    org_id = None
    clerk_org = f"clerk_org_{uuid.uuid4().hex[:8]}"
    clerk_user = f"clerk_{uuid.uuid4().hex[:8]}"

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity:
        return ClerkGithubIdentity("octocat", "o@e.com", "583231")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)

    event = {
        "type": "organizationMembership.created",
        "data": {
            "organization": {"id": clerk_org, "name": "Acme", "slug": "acme"},
            "user_id": clerk_user,
            "public_user_data": {"identifier": "member@e.com"},
            "role": "org:member",
        },
    }
    monkeypatch.setattr(webhooks, "_verify_clerk_webhook", lambda *_a, **_k: event)

    class _Req:
        headers: dict = {}

        async def body(self) -> bytes:
            return b"{}"

    try:
        await webhooks.handle_clerk_webhook(_Req())
        async with get_session() as session:
            org = await session.execute(
                OrganizationModel.__table__.select().where(
                    OrganizationModel.clerk_org_id == clerk_org
                )
            )
            org_id = org.mappings().one()["id"]
            result = await session.execute(
                UserModel.__table__.select().where(
                    UserModel.clerk_user_id == clerk_user
                )
            )
            row = result.mappings().one()
        assert row["github_id"] == "583231"
        assert row["github_username"] == "octocat"
    finally:
        if org_id is not None:
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
            github_id_checked_at=datetime.now(timezone.utc),
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
