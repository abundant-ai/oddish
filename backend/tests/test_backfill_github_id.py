from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import httpx

import auth.provisioning as prov
import backfill_github_id as job
from auth.provisioning import GITHUB_ID_RECHECK_TTL, ClerkGithubIdentity
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _clerk_secret_set(monkeypatch):
    """The backfill now short-circuits when CLERK_SECRET_KEY is unset. Every test
    that exercises the fetch path needs a secret present; the secret-unset test
    overrides this in its own body."""
    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")


def _mock_clerk_http(monkeypatch, handler) -> None:
    """Drive the REAL fetch_github_identity_from_clerk through a mocked HTTP
    transport so the backfill exercises the 404/500 status contract, not just the
    identity seam."""
    real_client = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
    monkeypatch.setattr(prov.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(
        job, "fetch_github_identity_from_clerk", prov.fetch_github_identity_from_clerk
    )


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


@pytest_asyncio.fixture
async def org_with_users():
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))

    async def add(**overrides) -> UserModel:
        user = _user(org_id, **overrides)
        async with get_session() as session:
            session.add(user)
        return user

    try:
        yield org_id, add
    finally:
        await _purge(org_id)


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
async def test_single_run_pages_past_a_full_batch_of_skips(
    monkeypatch, org_with_users
) -> None:
    """An uncapped run keeps paging past a full batch of unfillable rows and
    fills a later user in the SAME call (drain mode must not stop at one batch)."""
    org_id, add = org_with_users
    skips = [await add(id=f"user_bf_a{i}") for i in range(3)]
    fillable = await add(id="user_bf_z")
    mapping = {u.clerk_user_id: ClerkGithubIdentity(None, None, None) for u in skips}
    mapping[fillable.clerk_user_id] = ClerkGithubIdentity("octocat", None, "42424242")
    _mock_clerk(monkeypatch, mapping)
    summary = await asyncio.wait_for(
        job.backfill_github_id(batch_size=3, delay_seconds=0.0), timeout=15
    )
    assert await _github_id_of(fillable.id) == "42424242"
    assert await _github_id_of(skips[0].id) is None
    assert summary.set >= 1
    assert summary.skipped >= 3


@requires_db
@pytest.mark.asyncio
async def test_full_head_batch_stamped_then_tail_reached_next_run(
    monkeypatch, org_with_users
) -> None:
    """A full head batch of no-github rows is stamped and skipped next run."""
    org_id, add = org_with_users
    head = [await add(id=f"user_bf_a{i}") for i in range(3)]
    tail = await add(id="user_bf_z")
    mapping: dict[str, ClerkGithubIdentity | None] = {
        u.clerk_user_id: ClerkGithubIdentity(None, None, None) for u in head
    }
    mapping[tail.clerk_user_id] = ClerkGithubIdentity("octocat", None, "tailid")
    _mock_clerk(monkeypatch, mapping)
    first = await asyncio.wait_for(
        job.backfill_github_id(batch_size=3, max_users=3, delay_seconds=0.0),
        timeout=15,
    )
    assert first.scanned == 3
    assert await _github_id_of(tail.id) is None
    for u in head:
        cache = await _cache_of(u.id)
        assert isinstance(cache, dict) and isinstance(
            cache.get("github_id_checked"), str
        )

    second = await job.backfill_github_id(
        batch_size=3, max_users=3, delay_seconds=0.0
    )
    assert await _github_id_of(tail.id) == "tailid"
    assert second.set >= 1


@requires_db
@pytest.mark.asyncio
async def test_identity_none_failure_logs_cause_not_bare_none(monkeypatch, caplog) -> None:
    """The dominant non-exception failure mode (Clerk returns None: unset secret /
    transient / non-github) must log a real cause + clerk_user_id, not the
    misleading 'Clerk fetch failed for user X: None' that leaves on-call blind."""
    import logging

    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(monkeypatch, {user.clerk_user_id: None})
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        with caplog.at_level(logging.WARNING, logger="backfill_github_id"):
            summary = await job.backfill_github_id(delay_seconds=0.0)
        assert summary.failed >= 1
        failures = [
            r for r in caplog.records if "Clerk fetch failed" in r.message
        ]
        assert failures, "expected a Clerk-fetch-failed log record"
        rendered = failures[0].getMessage()
        assert user.clerk_user_id in rendered
        assert not rendered.rstrip().endswith(": None")
        assert "no identity" in rendered
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_exception_failure_still_logs_exception(monkeypatch, caplog) -> None:
    """When an exception escapes the fetch, the log still carries the exception
    repr (not the identity-None cause) so genuine crashes stay diagnosable."""
    import logging

    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)

    async def _boom(_clerk_user_id: str) -> ClerkGithubIdentity:
        raise RuntimeError("clerk kaboom")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _boom)
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        with caplog.at_level(logging.WARNING, logger="backfill_github_id"):
            summary = await job.backfill_github_id(delay_seconds=0.0)
        assert summary.failed >= 1
        failures = [r for r in caplog.records if "Clerk fetch failed" in r.message]
        assert failures
        rendered = failures[0].getMessage()
        assert "clerk kaboom" in rendered
        assert user.clerk_user_id in rendered
    finally:
        await _purge(org_id)


def _stale_marker() -> str:
    stale = datetime.now(timezone.utc) - GITHUB_ID_RECHECK_TTL - timedelta(minutes=5)
    return stale.isoformat()


def _fresh_marker() -> str:
    return datetime.now(timezone.utc).isoformat()


@requires_db
@pytest.mark.asyncio
async def test_stale_marker_rows_reenter_scan_and_fill(monkeypatch) -> None:
    """Self-heal: a row stamped checked-absent longer ago than the TTL re-enters the
    scan and gets its github_id filled once Clerk now returns one."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id, attribution_cache={"github_id_checked": _stale_marker()})
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", None, "healed")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert summary.set == 1
        assert await _github_id_of(user.id) == "healed"
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_stale_marker_still_no_github_restamped(monkeypatch) -> None:
    """A stale-marker row that Clerk still reports as no-github re-enters the scan
    and is re-stamped with a fresh timestamp."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    old = _stale_marker()
    user = _user(org_id, attribution_cache={"github_id_checked": old})
    _mock_clerk(monkeypatch, {user.clerk_user_id: ClerkGithubIdentity(None, None, None)})
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert summary.scanned >= 1
        cache = await _cache_of(user.id)
        assert isinstance(cache, dict)
        assert cache.get("github_id_checked") > old
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_clerk_404_user_failed_not_stamped_rescanned(monkeypatch) -> None:
    """A Clerk 404 is NON-definitive (indistinguishable from a wrong-instance
    CLERK_SECRET_KEY): run 1 counts it failed, stamps NO marker, and leaves
    github_id untouched; the row is re-selected on the next run. Genuinely
    deleted Clerk users are soft-deleted by the membership webhook, so the
    retry cost is bounded."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk_http(monkeypatch, lambda _req: httpx.Response(404))
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.failed >= 1
        assert first.skipped == 0
        assert await _github_id_of(user.id) is None
        cache = await _cache_of(user.id)
        assert not (isinstance(cache, dict) and "github_id_checked" in cache)
        second = await job.backfill_github_id(delay_seconds=0.0)
        assert second.scanned >= 1  # re-scanned, not excluded
    finally:
        await _purge(org_id)


@requires_db
@pytest.mark.asyncio
async def test_clerk_500_user_failed_not_stamped_rescanned(monkeypatch) -> None:
    """A transient Clerk error (500) is non-definitive: run 1 counts it failed and
    stamps nothing; a later definitive fetch fills the id."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk_http(monkeypatch, lambda _req: httpx.Response(500))
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.failed >= 1
        assert first.skipped == 0
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


@requires_db
@pytest.mark.asyncio
async def test_username_no_id_not_stamped_rescanned_then_filled(monkeypatch) -> None:
    """A GitHub account exists in Clerk (username) but the id wasn't reported in
    this response (partial answer): run 1 counts it skipped and stamps NOTHING;
    the row is re-scanned on a later run, and once Clerk returns the id it fills."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    user = _user(org_id)
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", None, None)},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        first = await job.backfill_github_id(delay_seconds=0.0)
        assert first.skipped >= 1
        assert await _github_id_of(user.id) is None
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


@pytest.mark.asyncio
async def test_secret_unset_short_circuits(monkeypatch, caplog) -> None:
    """When CLERK_SECRET_KEY is unset the run returns an empty summary without
    querying the DB or attempting any Clerk fetch — no per-row failure logs."""
    import logging

    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "")
    calls = 0

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity:
        nonlocal calls
        calls += 1
        return ClerkGithubIdentity("octocat", None, "555")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)
    with caplog.at_level(logging.WARNING, logger="backfill_github_id"):
        summary = await job.backfill_github_id(delay_seconds=0.0)
    assert summary.scanned == 0
    assert summary.set == 0
    assert summary.failed == 0
    assert calls == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@requires_db
@pytest.mark.asyncio
async def test_exhausted_time_budget_stops_before_first_batch(
    monkeypatch, org_with_users
) -> None:
    """The loop-top budget check: an already-exhausted budget stops the run
    before any batch is selected or fetched — nothing scanned, nothing written."""
    org_id, add = org_with_users
    users = [await add(id=f"user_bf_a{i}") for i in range(3)]
    calls = 0

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal calls
        calls += 1
        return ClerkGithubIdentity("h", None, "should-not-apply")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)
    summary = await asyncio.wait_for(
        job.backfill_github_id(delay_seconds=0.0, time_budget_seconds=0.0),
        timeout=15,
    )
    assert summary.scanned == 0
    assert calls == 0
    for u in users:
        assert await _github_id_of(u.id) is None


@requires_db
@pytest.mark.asyncio
async def test_all_failed_batch_stops_run_early(monkeypatch, org_with_users) -> None:
    """A full batch of failing fetches (>= floor) aborts the run: batch 2 is never
    scanned even though more eligible rows exist."""
    org_id, add = org_with_users
    users = [await add(id=f"user_bf_{i:03d}") for i in range(15)]
    assert job.ALL_FAILED_ABORT_FLOOR <= 10

    async def _fail(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return None

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fail)
    summary = await asyncio.wait_for(
        job.backfill_github_id(batch_size=10, delay_seconds=0.0),
        timeout=15,
    )
    # Only the first batch of 10 was scanned; the run stopped before batch 2.
    assert summary.scanned == 10
    assert summary.failed == 10
    assert summary.set == 0


@requires_db
@pytest.mark.asyncio
async def test_all_failed_batch_below_floor_does_not_stop(
    monkeypatch, org_with_users
) -> None:
    """A tiny all-failed batch (< floor) must NOT trip the early stop, so a single
    flaky tail user cannot abort the whole scan."""
    org_id, add = org_with_users
    users = [await add(id=f"user_bf_{i:03d}") for i in range(6)]

    async def _fail(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return None

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fail)
    summary = await asyncio.wait_for(
        job.backfill_github_id(batch_size=3, delay_seconds=0.0),
        timeout=15,
    )
    # Batch size 3 < floor 10, so it keeps paging through every eligible row.
    assert summary.scanned == 6
    assert summary.failed == 6


@requires_db
@pytest.mark.asyncio
async def test_phase_c_skips_row_filled_between_select_and_write(
    monkeypatch, org_with_users
) -> None:
    """Phase-C precondition re-check: if a row's github_id is set between the
    Phase-A select and the Phase-B fetch, Phase C must skip it (never overwrite)
    even though Clerk returned a DIFFERENT id."""
    org_id, add = org_with_users
    user = await add()
    fetch_started = asyncio.Event()

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        # Signal that Phase A has completed and we're mid-fetch, then race a
        # concurrent write that fills github_id before Phase C runs.
        fetch_started.set()
        await asyncio.sleep(0.1)
        return ClerkGithubIdentity("octocat", None, "clerk-different")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)

    async def _fill_concurrently() -> None:
        await fetch_started.wait()
        async with get_session() as session:
            row = await session.get(UserModel, user.id)
            row.github_id = "already-set"

    run = asyncio.create_task(job.backfill_github_id(delay_seconds=0.0))
    await asyncio.wait_for(_fill_concurrently(), timeout=15)
    summary = await asyncio.wait_for(run, timeout=15)

    assert await _github_id_of(user.id) == "already-set"
    assert summary.set == 0
    assert summary.skipped >= 1


@requires_db
@pytest.mark.asyncio
async def test_phase_c_skips_row_stamped_checked_between_phases(
    monkeypatch, org_with_users
) -> None:
    """Phase-C precondition re-check: a definitive no-github stamp landing
    between phases (login refresh / webhook unlink) is a NEWER signal than the
    Phase-B fetch — the older fetched id must not be written over it."""
    org_id, add = org_with_users
    user = await add()
    fetch_started = asyncio.Event()

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        fetch_started.set()
        await asyncio.sleep(0.1)
        return ClerkGithubIdentity("octocat", None, "stale-answer")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)

    async def _stamp_concurrently() -> None:
        await fetch_started.wait()
        async with get_session() as session:
            row = await session.get(UserModel, user.id)
            prov._mark_github_id_checked(row)

    run = asyncio.create_task(job.backfill_github_id(delay_seconds=0.0))
    await asyncio.wait_for(_stamp_concurrently(), timeout=15)
    summary = await asyncio.wait_for(run, timeout=15)

    assert await _github_id_of(user.id) is None
    assert summary.set == 0
    assert summary.skipped >= 1


@requires_db
@pytest.mark.asyncio
async def test_phase_c_skips_row_relinked_to_other_clerk_user(
    monkeypatch, org_with_users
) -> None:
    """Phase-C precondition re-check: if the row's clerk_user_id changed between
    phases (email-based provisioning can rewrite it), the fetched identity
    belongs to someone else and must not be applied."""
    org_id, add = org_with_users
    user = await add()
    fetch_started = asyncio.Event()

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        fetch_started.set()
        await asyncio.sleep(0.1)
        return ClerkGithubIdentity("octocat", None, "old-clerk-users-id")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _fetch)

    async def _relink_concurrently() -> None:
        await fetch_started.wait()
        async with get_session() as session:
            row = await session.get(UserModel, user.id)
            row.clerk_user_id = f"clerk_other_{uuid.uuid4().hex[:8]}"

    run = asyncio.create_task(job.backfill_github_id(delay_seconds=0.0))
    await asyncio.wait_for(_relink_concurrently(), timeout=15)
    summary = await asyncio.wait_for(run, timeout=15)

    assert await _github_id_of(user.id) is None
    assert summary.set == 0
    assert summary.skipped >= 1


@requires_db
@pytest.mark.asyncio
async def test_time_budget_cancels_mid_batch(monkeypatch, org_with_users) -> None:
    """The budget is enforced INSIDE a batch, not just between batches: slow
    fetches are cancelled once the remaining budget elapses, the batch's results
    are dropped (nothing written), and the run ends."""
    org_id, add = org_with_users
    users = [await add(id=f"user_bf_{i}") for i in range(4)]

    async def _hang(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        await asyncio.sleep(30)
        return ClerkGithubIdentity("octocat", None, "never-applied")

    monkeypatch.setattr(job, "fetch_github_identity_from_clerk", _hang)
    summary = await asyncio.wait_for(
        job.backfill_github_id(
            batch_size=4, delay_seconds=0.0, time_budget_seconds=0.3
        ),
        timeout=10,
    )
    assert summary.set == 0
    for u in users:
        assert await _github_id_of(u.id) is None


@requires_db
@pytest.mark.asyncio
async def test_fresh_marker_rows_excluded_from_scan(monkeypatch) -> None:
    """A row stamped checked-absent within the TTL stays excluded from the scan."""
    org_id = f"org_bf_{uuid.uuid4().hex[:8]}"
    fresh = _fresh_marker()
    user = _user(org_id, attribution_cache={"github_id_checked": fresh})
    _mock_clerk(
        monkeypatch,
        {user.clerk_user_id: ClerkGithubIdentity("octocat", None, "shouldnotfill")},
    )
    try:
        async with get_session() as session:
            session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
            session.add(user)
        summary = await job.backfill_github_id(delay_seconds=0.0)
        assert summary.scanned == 0
        assert await _github_id_of(user.id) is None
        cache = await _cache_of(user.id)
        assert isinstance(cache, dict) and cache.get("github_id_checked") == fresh
    finally:
        await _purge(org_id)
