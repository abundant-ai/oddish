from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import asyncpg

from oddish.config import settings


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
        await conn.execute(
            """
            INSERT INTO queue_slots (queue_key, slot)
            SELECT $1, slot
            FROM generate_series(0, $2 - 1) AS slot
            ON CONFLICT DO NOTHING
            """,
            queue_key,
            limit,
        )


async def acquire_queue_slot(
    *,
    queue_key: str,
    limit: int,
    worker_id: str,
    lease_seconds: int,
) -> int | None:
    """Acquire a queue slot lease without holding a session connection."""
    if limit <= 0:
        return None
    await ensure_queue_slots(queue_key, limit)
    async with _slot_connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT queue_key, slot
                    FROM queue_slots
                    WHERE queue_key = $1
                      AND slot < $2
                      AND (locked_until IS NULL OR locked_until <= NOW())
                    ORDER BY slot
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE queue_slots
                SET locked_by = $3,
                    locked_until = NOW() + make_interval(secs => $4),
                    locked_at = NOW()
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
                locked_at = NULL
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
                locked_at = NULL
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
