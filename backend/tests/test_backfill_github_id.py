from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

import backfill_github_id as job
from auth.provisioning import ClerkGithubIdentity
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _mock_clerk(monkeypatch, mapping: dict[str, ClerkGithubIdentity]) -> None:
    """Seam: fetch_github_identity_from_clerk keyed by clerk_user_id."""

    async def _fetch(clerk_user_id: str) -> ClerkGithubIdentity:
        return mapping.get(clerk_user_id, ClerkGithubIdentity(None, None, None))

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)


def _user(org_id: str, **overrides) -> UserModel:
    base = {
        "id": f"user_{uuid.uuid4().hex[:8]}",
        "org_id": org_id,
        "email": f"{uuid.uuid4().hex[:8]}@e.com",
        "github_username": "octocat",
        "github_id": None,
        "clerk_user_id": f"clerk_{uuid.uuid4().hex[:8]}",
        "role": UserRole.MEMBER,
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


async def _purge(org_id: str) -> None:
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


async def _github_id_of(user_id: str) -> str | None:
    async with get_session() as session:
        row = await session.get(UserModel, user_id)
        return row.github_id if row else None


@requires_db
@pytest.mark.asyncio
async def test_backfills_null_github_id(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", "o@e.com", "555")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert await _github_id_of(user.id) == "555"
        assert summary.set == 1
        assert summary.scanned >= 1
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_does_not_overwrite_existing_github_id(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id, github_id="original")
    # Clerk returns a DIFFERENT id; the null filter should skip this row entirely.
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", None, "different")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert await _github_id_of(user.id) == "original"
        assert summary.set == 0
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_skips_users_without_clerk_user_id(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id, clerk_user_id=None)
    _mock_clerk(monkeypatch, {})
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert await _github_id_of(user.id) is None
        assert summary.set == 0
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_respects_max_users_cap(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    users = [_user(org_id) for _ in range(3)]
    _mock_clerk(
        monkeypatch,
        {u.clerk_user_id: ClerkGithubIdentity("h", None, f"id_{i}") for i, u in enumerate(users)},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            for u in users:
                session.add(u)
        summary = await job.backfill_github_id(
            max_users=2, batch_size=2, delay_seconds=0.0
        )
        assert summary.scanned == 2
        set_count = 0
        for u in users:
            if await _github_id_of(u.id) is not None:
                set_count += 1
        assert set_count == 2
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_fail_open_one_clerk_error_does_not_abort(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    bad = _user(org_id)
    good = _user(org_id)

    async def _fetch(clerk_user_id: str) -> ClerkGithubIdentity:
        if clerk_user_id == bad.clerk_user_id:
            raise RuntimeError("clerk boom")
        return ClerkGithubIdentity("h", None, "goodid")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(bad)
            session.add(good)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert await _github_id_of(good.id) == "goodid"
        assert await _github_id_of(bad.id) is None
        assert summary.failed == 1
        assert summary.set == 1
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_idempotent_second_run_is_noop(monkeypatch) -> None:
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", None, "777")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.set == 1
        second = await job.backfill_github_id(delay_seconds=0.0)
        assert second.scanned == 0
        assert second.set == 0
        assert await _github_id_of(user.id) == "777"
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_concurrent_batch_backfills_all_users(monkeypatch) -> None:
    """More users than the concurrency cap in one batch: Clerk GETs run
    concurrently but DB writes are serialized on the single batch session, so
    every user is set with no concurrent-AsyncSession error."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    users = [_user(org_id) for _ in range(8)]
    _mock_clerk(
        monkeypatch,
        {u.clerk_user_id: ClerkGithubIdentity("h", None, f"cid_{i}") for i, u in enumerate(users)},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            for u in users:
                session.add(u)
        summary = await job.backfill_github_id(
            batch_size=8, concurrency=4, delay_seconds=0.0
        )
        assert summary.set == 8
        for u in users:
            assert await _github_id_of(u.id) is not None
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_collision_with_soft_deleted_holder_is_skipped(monkeypatch) -> None:
    """A github_id already claimed by a soft-deleted org member must be skipped
    (via _set_github_id_if_absent's include_deleted pre-check), not crash."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    holder = _user(
        org_id,
        github_id="taken",
        is_active=False,
        deleted_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    target = _user(org_id)
    _mock_clerk(
        monkeypatch,
        {target.clerk_user_id: ClerkGithubIdentity("octocat", None, "taken")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(holder)
            session.add(target)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert await _github_id_of(target.id) is None
        assert summary.skipped >= 1
    finally:
        await _purge(org_id)
