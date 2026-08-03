from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from oddish.workers.queue import provider_capacity


requires_db = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"),
    reason="ODDISH_DATABASE_URL not set",
)


@pytest_asyncio.fixture
async def provider_name():
    provider = f"capacity-test-{uuid.uuid4().hex}"
    yield provider
    async with provider_capacity._capacity_connection() as conn:
        await conn.execute(
            "DELETE FROM provider_capacity_leases WHERE provider = $1", provider
        )


async def _try(
    provider: str,
    owner: str,
    memory: int,
    *,
    limit=10,
    count=10,
    lease_seconds=300,
    waiter_seconds=60,
):
    return await provider_capacity._try_acquire(
        provider=provider,
        owner_id=owner,
        requested_memory_mb=memory,
        memory_limit_mb=limit,
        lease_limit=count,
        lease_seconds=lease_seconds,
        waiter_seconds=waiter_seconds,
    )


@requires_db
@pytest.mark.asyncio
async def test_memory_and_count_limits_are_shared_across_queue_owners(provider_name):
    acquired_a, _ = await _try(provider_name, "queue-a:job-a", 7, limit=10, count=2)
    acquired_b, snapshot = await _try(
        provider_name, "queue-b:job-b", 4, limit=10, count=2
    )
    assert acquired_a is True
    assert acquired_b is False
    assert (snapshot.used_memory_mb, snapshot.held_leases) == (7, 1)

    await provider_capacity._release(provider=provider_name, owner_id="queue-a:job-a")
    acquired_b, snapshot = await _try(
        provider_name, "queue-b:job-b", 4, limit=10, count=2
    )
    assert acquired_b is True
    assert (snapshot.used_memory_mb, snapshot.held_leases) == (4, 1)

    acquired_c, _ = await _try(provider_name, "queue-c:job-c", 1, limit=10, count=1)
    assert acquired_c is False


@requires_db
@pytest.mark.asyncio
async def test_fifo_waiter_cannot_be_bypassed(provider_name):
    assert (await _try(provider_name, "owner-a", 10))[0]
    assert not (await _try(provider_name, "owner-b", 8))[0]
    await asyncio.sleep(0.01)
    assert not (await _try(provider_name, "owner-c", 1))[0]

    await provider_capacity._release(provider=provider_name, owner_id="owner-a")
    assert not (await _try(provider_name, "owner-c", 1))[0]
    assert (await _try(provider_name, "owner-b", 8))[0]


@requires_db
@pytest.mark.asyncio
async def test_dead_head_waiter_expires_short_while_held_lease_keeps_grace(
    provider_name,
):
    assert (
        await _try(
            provider_name,
            "held-owner",
            10,
            lease_seconds=7200,
            waiter_seconds=60,
        )
    )[0]
    assert not (
        await _try(
            provider_name,
            "dead-head",
            1,
            lease_seconds=7200,
            waiter_seconds=60,
        )
    )[0]
    # Exercise both ON CONFLICT refresh branches: a live holder keeps the long
    # teardown lease while a live waiter keeps only its short queue-entry TTL.
    assert (
        await _try(
            provider_name,
            "held-owner",
            10,
            lease_seconds=7200,
            waiter_seconds=60,
        )
    )[0]
    assert not (
        await _try(
            provider_name,
            "dead-head",
            1,
            lease_seconds=7200,
            waiter_seconds=60,
        )
    )[0]

    async with provider_capacity._capacity_connection() as conn:
        expiries = await conn.fetchrow(
            """
            SELECT EXTRACT(EPOCH FROM (held.lease_expires_at - NOW())) AS held_ttl,
                   EXTRACT(EPOCH FROM (waiter.lease_expires_at - NOW())) AS waiter_ttl
            FROM provider_capacity_leases held
            JOIN provider_capacity_leases waiter ON waiter.provider = held.provider
            WHERE held.provider = $1 AND held.owner_id = 'held-owner'
              AND waiter.owner_id = 'dead-head'
            """,
            provider_name,
        )
        await conn.execute(
            """
            UPDATE provider_capacity_leases
            SET lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE provider = $1 AND owner_id = 'dead-head'
            """,
            provider_name,
        )

    assert float(expiries["held_ttl"]) > 7000
    assert 0 < float(expiries["waiter_ttl"]) <= 60

    await provider_capacity._release(
        provider=provider_name,
        owner_id="held-owner",
    )
    assert (
        await _try(
            provider_name,
            "next-owner",
            1,
            lease_seconds=7200,
            waiter_seconds=60,
        )
    )[0]


@requires_db
@pytest.mark.asyncio
async def test_concurrent_admission_never_exceeds_weighted_budget(provider_name):
    results = await asyncio.gather(
        *[
            _try(provider_name, f"owner-{index:02d}", 3, limit=10, count=100)
            for index in range(20)
        ]
    )
    assert sum(acquired for acquired, _ in results) == 3
    async with provider_capacity._capacity_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS held, SUM(requested_memory_mb) AS used
            FROM provider_capacity_leases
            WHERE provider = $1 AND state = 'HELD'
            """,
            provider_name,
        )
    assert (int(row["held"]), int(row["used"])) == (3, 9)


@requires_db
@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_before_admission(provider_name):
    async with provider_capacity._capacity_connection() as conn:
        await conn.execute(
            """
            INSERT INTO provider_capacity_leases (
                provider, owner_id, requested_memory_mb, state,
                heartbeat_at, lease_expires_at
            ) VALUES ($1, 'dead-owner', 10, 'HELD', NOW() - INTERVAL '10 minutes',
                      NOW() - INTERVAL '1 minute')
            """,
            provider_name,
        )
    acquired, snapshot = await _try(provider_name, "new-owner", 10)
    assert acquired is True
    assert (snapshot.held_leases, snapshot.used_memory_mb) == (1, 10)


@requires_db
@pytest.mark.asyncio
async def test_cleanup_removes_stale_waiters_and_held_leases(provider_name):
    async with provider_capacity._capacity_connection() as conn:
        for owner, state in (("dead-waiter", "WAITING"), ("dead-holder", "HELD")):
            await conn.execute(
                """
                INSERT INTO provider_capacity_leases (
                    provider, owner_id, requested_memory_mb, state,
                    heartbeat_at, lease_expires_at
                ) VALUES ($1, $2, 1, $3, NOW() - INTERVAL '10 minutes',
                          NOW() - INTERVAL '1 minute')
                """,
                provider_name,
                owner,
                state,
            )
    assert await provider_capacity.cleanup_stale_provider_capacity_leases() >= 2
    async with provider_capacity._capacity_connection() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM provider_capacity_leases WHERE provider = $1",
            provider_name,
        )
    assert count == 0
