import json
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4
from contextlib import asynccontextmanager

import asyncpg

from oddish.config import settings
from oddish.workers.queue.worker_job_dispatcher import DispatchUnit, QueueDemandKey

if TYPE_CHECKING:
    from oddish.dispatch.cycle import DispatchPlan

# Covers cold starts and the 120s dispatcher timeout. Expired calls cannot adopt
# a replacement lease; their ownership token is fenced at worker startup.
LAUNCH_LEASE_SECONDS = 300


_ENSURE_QUEUE_SLOTS_SQL = """
    INSERT INTO queue_slots (queue_key, slot)
    SELECT $1, slot FROM generate_series(0, $2 - 1) AS slot
    ON CONFLICT DO NOTHING
"""


class LaunchReservation(NamedTuple):
    unit: DispatchUnit
    slot: int
    token: str


@asynccontextmanager
async def _slot_connection() -> AsyncIterator[asyncpg.Connection]:
    # Slot bookkeeping only needs short, one-off queries. Using direct
    # connections here avoids keeping an extra asyncpg pool connection open for
    # the full lifetime of every long-running Modal worker.
    conn = await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
        server_settings=settings.asyncpg_server_settings(),
    )
    try:
        yield conn
    finally:
        await conn.close()


async def ensure_queue_slots(queue_key: str, limit: int) -> None:
    """Ensure queue slot rows exist up to the configured limit."""
    if limit <= 0:
        return
    async with _slot_connection() as conn:
        await conn.execute(_ENSURE_QUEUE_SLOTS_SQL, queue_key, limit)


async def acquire_queue_slot(
    *,
    queue_key: str,
    limit: int,
    worker_id: str,
    lease_seconds: int,
    reservation_token: str | None = None,
) -> int | None:
    """Acquire a queue slot lease without holding a session connection."""
    if limit <= 0:
        return None
    if reservation_token is not None:
        async with _slot_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE queue_slots SET locked_by = $3,
                    locked_until = NOW() + make_interval(secs => $4),
                    locked_at = NOW(),
                    launch_demand = jsonb_set(launch_demand, '{adopted}', 'true')
                WHERE queue_key = $1 AND slot < $2 AND locked_by = $5
                  AND launch_demand IS NOT NULL AND locked_until > NOW()
                  AND NOT (launch_demand->>'adopted')::boolean
                RETURNING slot
                """,
                queue_key,
                limit,
                worker_id,
                lease_seconds,
                reservation_token,
            )
        return int(row["slot"]) if row else None
    await ensure_queue_slots(queue_key, limit)
    async with _slot_connection() as conn:
        async with conn.transaction():
            # Serialize acquisitions for this model, including slots above a
            # newly lowered limit. Slot numbers alone are not a capacity count.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 71))", queue_key
            )
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT queue_key, slot
                    FROM queue_slots
                    WHERE queue_key = $1
                      AND slot < $2
                      AND (SELECT COUNT(*) FROM queue_slots
                           WHERE queue_key = $1 AND locked_by IS NOT NULL
                             AND (locked_until IS NULL OR locked_until > NOW())) < $2
                      AND (locked_until IS NULL OR locked_until <= NOW())
                    ORDER BY slot
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE queue_slots
                SET locked_by = $3,
                    locked_until = NOW() + make_interval(secs => $4),
                    locked_at = NOW(), launch_demand = NULL
                FROM candidate
                WHERE queue_slots.queue_key = candidate.queue_key
                  AND queue_slots.slot = candidate.slot
                RETURNING queue_slots.slot
                """,
                queue_key,
                limit,
                worker_id,
                lease_seconds,
            )
    if row is None:
        return None
    return int(row["slot"])


async def release_queue_slot(
    *,
    queue_key: str,
    slot: int,
    worker_id: str,
) -> None:
    async with _slot_connection() as conn:
        await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL,
                locked_until = NULL,
                locked_at = NULL, launch_demand = NULL
            WHERE queue_key = $1
              AND slot = $2
              AND locked_by = $3
            """,
            queue_key,
            slot,
            worker_id,
        )
    # A freed slot may unblock a waiting job: wake the in-process dispatcher
    # (best-effort, no-op when no loop runs here; the fallback poll backstops).
    try:
        from oddish.dispatch.cycle import signal_dispatch

        signal_dispatch()
    except Exception:
        pass


async def count_held_queue_slots(queue_keys: Sequence[str]) -> dict[str, int]:
    """Per-``queue_key`` count of currently HELD slot leases.

    A slot is held when it carries a ``locked_by`` and its lease has not expired
    (the same ``locked_until`` freshness test ``acquire_queue_slot`` uses). This
    is the authoritative in-flight concurrency for a ``queue_key``: the lease is
    taken at spawn/claim, *before* the job flips to RUNNING in ``worker_jobs``.

    The off-Modal dispatch cycle folds this into its planning so a fast
    event-trigger re-fire (before freshly-spawned workers register as RUNNING)
    does not over-spawn workers that then lose the ``queue_slots`` race and exit.
    Filtered to ``queue_keys`` (mirrors ``get_worker_job_org_queue_counts``); an
    empty request short-circuits to ``{}`` without a round-trip.
    """
    if not queue_keys:
        return {}
    async with _slot_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT queue_key, COUNT(*) AS held
            FROM   queue_slots
            WHERE  locked_by IS NOT NULL
              AND  (locked_until IS NULL OR locked_until > NOW())
              AND  queue_key = ANY($1)
            GROUP BY queue_key
            """,
            list(queue_keys),
        )
    return {str(row["queue_key"]): int(row["held"] or 0) for row in rows}


async def cleanup_stale_queue_slots() -> int:
    """Clear expired slot leases so admin views stay accurate."""
    async with _slot_connection() as conn:
        result = await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL,
                locked_until = NULL,
                locked_at = NULL, launch_demand = NULL
            WHERE locked_by IS NOT NULL
              AND locked_until IS NOT NULL
              AND locked_until <= NOW()
            """
        )
    # asyncpg returns command tag strings like: "UPDATE <n>"
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


async def reserve_queue_launches(
    build_plan: Callable[..., Awaitable["DispatchPlan"]],
) -> tuple["DispatchPlan", list[LaunchReservation]]:
    """Commit planning cursors and short slot leases before any remote launch.

    One DB row serializes hosted planners. Ordinary worker slot acquisitions
    also take the per-model advisory lock, so stale plans cannot over-reserve.
    The callback may only read/plan database state, never launch remote work.
    """
    async with _slot_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO queue_dispatch_state (id) VALUES (1) ON CONFLICT DO NOTHING"
            )
            raw = await conn.fetchval(
                "SELECT cursors FROM queue_dispatch_state WHERE id = 1 FOR UPDATE"
            )
            cursors = json.loads(raw)
            rows = await conn.fetch(
                """
                SELECT launch_demand FROM queue_slots
                WHERE launch_demand IS NOT NULL AND locked_until > NOW()
                """
            )
            pending = Counter(
                tuple(json.loads(row["launch_demand"])["key"]) for row in rows
            )
            plan = await build_plan(
                fairness_cursors=cursors, pending_by_org_queue=pending
            )
            by_queue = defaultdict(list)
            for unit in plan.unit_plan:
                by_queue[unit.queue_key].append(unit)
            reserved: list[LaunchReservation] = []
            updates = []
            # Stable lock order also permits multiple scheduler hosts safely.
            for key, units in sorted(by_queue.items()):
                limit = plan.concurrency_limits.get(key, 0)
                if limit <= 0:
                    continue
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 71))", key
                )
                await conn.execute(
                    _ENSURE_QUEUE_SLOTS_SQL,
                    key,
                    limit,
                )
                held = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM queue_slots
                    WHERE queue_key = $1 AND locked_by IS NOT NULL
                      AND (locked_until IS NULL OR locked_until > NOW())
                    """,
                    key,
                )
                slots = await conn.fetch(
                    """
                    SELECT slot FROM queue_slots
                    WHERE queue_key = $1 AND slot < $2
                      AND (locked_until IS NULL OR locked_until <= NOW())
                    ORDER BY slot FOR UPDATE SKIP LOCKED LIMIT $3
                    """,
                    key,
                    limit,
                    max(0, min(len(units), limit - held)),
                )
                for unit, row in zip(units, slots):
                    token = str(uuid4())
                    demand: QueueDemandKey = (
                        unit.org_id,
                        key,
                        unit.harbor_variant_id,
                        unit.execution_lane,
                        unit.priority_class,
                    )
                    reserved.append(LaunchReservation(unit, row["slot"], token))
                    updates.append(
                        (
                            key,
                            row["slot"],
                            token,
                            LAUNCH_LEASE_SECONDS,
                            json.dumps({"key": demand, "adopted": False}),
                        )
                    )
            if updates:
                await conn.executemany(
                    """
                    UPDATE queue_slots
                    SET locked_by = $3,
                        locked_until = NOW() + make_interval(secs => $4),
                        locked_at = NOW(), launch_demand = $5::jsonb
                    WHERE queue_key = $1 AND slot = $2
                    """,
                    updates,
                )
            await conn.execute(
                "UPDATE queue_dispatch_state SET cursors = $1::jsonb WHERE id = 1",
                json.dumps(cursors),
            )
    return plan, reserved


async def release_launch_reservations(tokens: Sequence[str]) -> None:
    """Release only unadopted tokens; never unlock a worker that already started."""
    if not tokens:
        return
    async with _slot_connection() as conn:
        await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL, locked_until = NULL,
                locked_at = NULL, launch_demand = NULL
            WHERE locked_by = ANY($1::text[]) AND launch_demand IS NOT NULL
            """,
            list(tokens),
        )
