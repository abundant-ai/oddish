from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

import auth.provisioning as prov
from auth.provisioning import (
    ClerkGithubIdentity,
    _github_account_from_clerk_payload,
    _refresh_user_github_identity,
    get_or_create_user_in_org,
)
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _payload(**account_overrides) -> dict:
    account = {
        "provider": "oauth_github",
        "username": "octocat",
        "email_address": "octo@example.com",
        "provider_user_id": "583231",
    }
    account.update(account_overrides)
    return {"external_accounts": [account]}


def test_payload_parse_extracts_github_id() -> None:
    identity = _github_account_from_clerk_payload(_payload())
    assert identity.username == "octocat"
    assert identity.email == "octo@example.com"
    assert identity.github_id == "583231"


def test_payload_parse_github_id_missing_is_none() -> None:
    identity = _github_account_from_clerk_payload(_payload(provider_user_id=None))
    assert identity.username == "octocat"
    assert identity.github_id is None


def test_payload_parse_no_github_account() -> None:
    identity = _github_account_from_clerk_payload({"external_accounts": []})
    assert identity == ClerkGithubIdentity(None, None, None)


def _mock_clerk(monkeypatch, identity: ClerkGithubIdentity | None) -> None:
    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return identity

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)


def _user(**overrides) -> UserModel:
    base = {
        "id": f"user_{uuid.uuid4().hex[:8]}",
        "org_id": "org_1",
        "email": "u@example.com",
        "github_username": None,
        "github_id": None,
        "clerk_user_id": "clerk_1",
        "role": UserRole.MEMBER,
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


@pytest.mark.asyncio
async def test_refresh_sets_github_id_when_missing(monkeypatch) -> None:
    user = _user()
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="octocat", email="o@e.com", github_id="42"),
    )
    await _refresh_user_github_identity(user)
    assert user.github_id == "42"
    assert user.github_username == "octocat"


@pytest.mark.asyncio
async def test_refresh_does_not_overwrite_differing_github_id(monkeypatch) -> None:
    user = _user(github_id="original", github_username="octocat")
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="octocat", email=None, github_id="different"),
    )
    await _refresh_user_github_identity(user)
    assert user.github_id == "original"


@pytest.mark.asyncio
async def test_refresh_no_clerk_call_once_github_id_known(monkeypatch) -> None:
    """Hot-path guard: a user with a github_username AND a known github_id (either
    the column is set or the checked marker is stamped) must NOT trigger a Clerk
    GET on refresh."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="999")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username="octocat",
        github_id="already-set",
        attribution_cache={"refreshed_at": "2020-01-01T00:00:00+00:00"},
    )
    await _refresh_user_github_identity(user)
    assert called is False


@pytest.mark.asyncio
async def test_refresh_fills_github_id_for_cached_pre_deploy_user(monkeypatch) -> None:
    """Login-fill regression: a pre-deploy user with a cached github_username and
    refreshed_at but NULL github_id must still get github_id filled on the next
    refresh (the old early-return starved these users forever)."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="777")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username="octocat",
        github_id=None,
        attribution_cache={
            "github_handles": ["octocat"],
            "legacy_emails": [],
            "refreshed_at": "2020-01-01T00:00:00+00:00",
        },
    )
    await _refresh_user_github_identity(user)
    assert called is True
    assert user.github_id == "777"


@pytest.mark.asyncio
async def test_refresh_stamps_marker_and_skips_second_fetch(monkeypatch) -> None:
    """After a definitive no-github answer, the checked marker is stamped so a
    second refresh does NOT re-fetch (no per-login Clerk storm)."""
    calls = 0

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal calls
        calls += 1
        return ClerkGithubIdentity(None, None, None)

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(github_username="octocat", github_id=None)
    await _refresh_user_github_identity(user)
    assert calls == 1
    assert isinstance(user.attribution_cache.get("github_id_checked"), str)
    await _refresh_user_github_identity(user)
    assert calls == 1  # marker short-circuits the second refresh
    assert user.github_id is None


@pytest.mark.asyncio
async def test_refresh_none_answer_stamps_nothing_then_fills_later(monkeypatch) -> None:
    """A None (Clerk error / secret unset) answer stamps nothing and is retried;
    a later definitive fetch fills the id."""
    result: ClerkGithubIdentity | None = None

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return result

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(github_username="octocat", github_id=None)
    await _refresh_user_github_identity(user)
    cache = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    assert "github_id_checked" not in cache
    assert user.github_id is None

    result = ClerkGithubIdentity(username="octocat", email=None, github_id="888")
    await _refresh_user_github_identity(user)
    assert user.github_id == "888"


@requires_db
@pytest.mark.asyncio
async def test_provisioning_sets_github_id_on_new_user(monkeypatch) -> None:
    org_id = f"org_gid_{uuid.uuid4().hex[:8]}"
    clerk_user_id = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="newby", email="n@e.com", github_id="999"),
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            await session.flush()
            org = await session.get(OrganizationModel, org_id)
            user = await get_or_create_user_in_org(
                session, clerk_user_id, org, "n@e.com", "member", UserRole.MEMBER
            )
            assert user.github_id == "999"
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.clerk_user_id == clerk_user_id
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_collision_does_not_crash(monkeypatch) -> None:
    """Two users in the same org would share a github_id (uq_users_org_github_id).
    Provisioning must stay fail-open: skip the colliding set, never raise."""
    org_id = f"org_gid_{uuid.uuid4().hex[:8]}"
    existing_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide"),
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(
                UserModel(
                    id=f"user_{uuid.uuid4().hex[:8]}",
                    org_id=org_id,
                    email="existing@e.com",
                    github_id="collide",
                    clerk_user_id=existing_clerk,
                    role=UserRole.MEMBER,
                    is_active=True,
                )
            )
            await session.flush()

        async with get_session() as session:
            org = await session.get(OrganizationModel, org_id)
            user = await get_or_create_user_in_org(
                session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
            )
            # Fail-open: the colliding github_id is not applied, no exception.
            assert user.github_id is None
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.org_id == org_id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_relink_releases_soft_deleted_holder(
    monkeypatch,
) -> None:
    """Leave-and-rejoin: a tombstoned row still holds the github_id. When the
    rejoining user's active row provisions, the soft-deleted holder must release
    the id (github_id -> None) so the new row can claim it. Otherwise the gate
    rejects the returning user forever."""
    org_id = f"org_gid_{uuid.uuid4().hex[:8]}"
    old_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide-sd"),
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(
                UserModel(
                    id=old_id,
                    org_id=org_id,
                    email="tombstone@e.com",
                    github_id="collide-sd",
                    clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                    role=UserRole.MEMBER,
                    is_active=False,
                    deleted_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                )
            )
            await session.flush()

        async with get_session() as session:
            org = await session.get(OrganizationModel, org_id)
            user = await get_or_create_user_in_org(
                session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
            )
            assert user.github_id == "collide-sd"  # relinked onto the active row
            released = await session.execute(
                UserModel.__table__.select()
                .where(UserModel.id == old_id)
                .execution_options(include_deleted=True)
            )
            assert released.mappings().one()["github_id"] is None
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete()
                .where(UserModel.org_id == org_id)
                .execution_options(include_deleted=True)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_active_clash_still_skips(monkeypatch) -> None:
    """An ACTIVE (not soft-deleted, is_active) clash row keeps its github_id and
    the new user is skipped — two live users must never share a github_id."""
    org_id = f"org_gid_{uuid.uuid4().hex[:8]}"
    holder_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide-live"),
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(
                UserModel(
                    id=holder_id,
                    org_id=org_id,
                    email="live@e.com",
                    github_id="collide-live",
                    clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                    role=UserRole.MEMBER,
                    is_active=True,
                )
            )
            await session.flush()

        async with get_session() as session:
            org = await session.get(OrganizationModel, org_id)
            user = await get_or_create_user_in_org(
                session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
            )
            assert user.github_id is None  # active holder keeps it
            holder = await session.get(UserModel, holder_id)
            assert holder.github_id == "collide-live"
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.org_id == org_id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_inactive_clash_releases(monkeypatch) -> None:
    """A clash row that is not soft-deleted but is_active=False also releases the
    id — deactivation without a tombstone must not gate a rejoining user."""
    org_id = f"org_gid_{uuid.uuid4().hex[:8]}"
    old_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide-inact"),
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(
                UserModel(
                    id=old_id,
                    org_id=org_id,
                    email="inactive@e.com",
                    github_id="collide-inact",
                    clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                    role=UserRole.MEMBER,
                    is_active=False,
                )
            )
            await session.flush()

        async with get_session() as session:
            org = await session.get(OrganizationModel, org_id)
            user = await get_or_create_user_in_org(
                session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
            )
            assert user.github_id == "collide-inact"
            holder = await session.get(UserModel, old_id)
            assert holder.github_id is None
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.org_id == org_id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )
