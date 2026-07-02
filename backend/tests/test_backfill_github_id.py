from __future__ import annotations

import asyncio
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


def _mock_clerk(
    monkeypatch, mapping: dict[str, ClerkGithubIdentity | None]
) -> None:
    """Seam: fetch_github_identity_from_clerk keyed by clerk_user_id. A None value
    models a non-definitive Clerk answer (error / secret unset)."""

    async def _fetch(clerk_user_id: str) -> ClerkGithubIdentity | None:
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


async def _cache_of(user_id: str) -> dict | None:
    async with get_session() as session:
        row = await session.get(UserModel, user_id)
        return row.attribution_cache if row else None


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
async def test_collision_with_soft_deleted_holder_relinks(monkeypatch) -> None:
    """A github_id claimed only by a soft-deleted org member is released to the
    active target during backfill (relink), not skipped forever."""
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
        assert await _github_id_of(target.id) == "taken"
        assert summary.set >= 1
        async with get_session() as session:
            released = await session.execute(
                UserModel.__table__.select()
                .where(UserModel.id == holder.id)
                .execution_options(include_deleted=True)
            )
            assert released.mappings().one()["github_id"] is None
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_full_batch_of_skips_terminates_and_reaches_later_users(monkeypatch) -> None:
    """Regression (keyset cursor): a FULL batch of unfillable users (Clerk returns
    no github_id, so they stay github_id IS NULL) must not re-select forever — an
    infinite loop in drain mode / starvation under a max_users cap. A fillable user
    ordered AFTER the skip prefix must still be reached."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    skips = [_user(org_id, id=f"user_bf_a{i}") for i in range(3)]
    fillable = _user(org_id, id="user_bf_z")
    mapping = {u.clerk_user_id: ClerkGithubIdentity(None, None, None) for u in skips}
    mapping[fillable.clerk_user_id] = ClerkGithubIdentity("octocat", None, "42424242")
    _mock_clerk(monkeypatch, mapping)
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            for u in [*skips, fillable]:
                session.add(u)
        # batch_size=3 => the first page is exactly the 3 skips (a full batch): the
        # pre-fix trigger for the non-terminating re-select. wait_for turns a hang
        # into a test failure instead of blocking the whole suite.
        summary = await asyncio.wait_for(
            job.backfill_github_id(batch_size=3, delay_seconds=0.0), timeout=15
        )
        assert await _github_id_of(fillable.id) == "42424242"  # reached, not starved
        assert await _github_id_of(skips[0].id) is None
        assert summary.set >= 1
        assert summary.skipped >= 3
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_stamped_rows_excluded_from_scan(monkeypatch) -> None:
    """A definitive no-github row is stamped github_id_checked in run 1; run 2 no
    longer selects it (the WHERE excludes stamped rows)."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(monkeypatch, {user.clerk_user_id: ClerkGithubIdentity(None, None, None)})
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.scanned >= 1
        assert first.skipped >= 1
        cache = await _cache_of(user.id)
        assert isinstance(cache, dict) and isinstance(
            cache.get("github_id_checked"), str
        )
        second = await job.backfill_github_id(delay_seconds=0.0)
        assert second.scanned == 0
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_head_of_table_no_github_batch_does_not_starve(monkeypatch) -> None:
    """Starvation regression: a full head batch of permanently-unfillable rows is
    stamped in run 1 (not just cursor-skipped); run 2 begins from after_id=None yet
    reaches rows beyond them because the stamped head no longer matches the scan."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    head = [_user(org_id, id=f"user_bf_a{i}") for i in range(3)]
    tail = _user(org_id, id="user_bf_z")
    mapping: dict[str, ClerkGithubIdentity | None] = {
        u.clerk_user_id: ClerkGithubIdentity(None, None, None) for u in head
    }
    mapping[tail.clerk_user_id] = ClerkGithubIdentity("octocat", None, "tailid")
    _mock_clerk(monkeypatch, mapping)
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            for u in [*head, tail]:
                session.add(u)
        # max_users=3 with batch_size=3: run 1 caps out on the head batch alone and
        # never reaches the tail. Pre-fix (no stamping) run 2 would re-scan the same
        # head and starve the tail forever.
        first = await job.backfill_github_id(
            batch_size=3, max_users=3, delay_seconds=0.0
        )
        assert first.scanned == 3
        assert await _github_id_of(tail.id) is None  # not reached in run 1
        for u in head:
            cache = await _cache_of(u.id)
            assert isinstance(cache, dict) and isinstance(
                cache.get("github_id_checked"), str
            )
        second = await job.backfill_github_id(
            batch_size=3, max_users=3, delay_seconds=0.0
        )
        assert await _github_id_of(tail.id) == "tailid"  # reached, not starved
        assert second.set >= 1
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_fetch_errors_not_stamped_and_rescanned(monkeypatch) -> None:
    """A non-definitive Clerk answer (None) stamps nothing and is re-scanned next
    run; a later definitive fetch then fills the id."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(monkeypatch, {user.clerk_user_id: None})
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.failed >= 1
        cache = await _cache_of(user.id)
        assert not (isinstance(cache, dict) and "github_id_checked" in cache)

        _mock_clerk(
            monkeypatch,
            {user.clerk_user_id: ClerkGithubIdentity("octocat", None, "laterid")},
        )
        second = await job.backfill_github_id(delay_seconds=0.0)
        assert second.scanned >= 1  # re-scanned, not excluded
        assert await _github_id_of(user.id) == "laterid"
    finally:
        await _purge(org_id)
