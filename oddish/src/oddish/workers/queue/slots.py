from collections.abc import AsyncIterator
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
                    locked_until = NOW() + make_interval(secs => $4)
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
                locked_until = NULL
            WHERE queue_key = $1
              AND slot = $2
              AND locked_by = $3
            """,
            queue_key,
            slot,
            worker_id,
        )


async def cleanup_stale_queue_slots() -> int:
    """Clear expired slot leases so admin views stay accurate."""
    async with _slot_connection() as conn:
        result = await conn.execute(
            """
            UPDATE queue_slots
            SET locked_by = NULL,
                locked_until = NULL
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


async def cleanup_orphaned_queue_slots(queue_key: str | None = None) -> int:
    """Release active leases that no RUNNING worker_job still owns.

    ``queue_slots`` is a coarse concurrency lease table. A worker can acquire a
    lease and then die before, or while, updating ``worker_jobs``. The stale
    expiry catches that eventually, but a long worker timeout can otherwise
    leave queued jobs at ``attempts=0`` because every new worker exits before it
    can claim. Match on the exact queue key, worker id, and slot so a healthy
    RUNNING job on the same model does not protect unrelated orphaned slots.
    """
    async with _slot_connection() as conn:
        result = await conn.execute(
            """
            UPDATE queue_slots qs
            SET    locked_by = NULL,
                   locked_until = NULL
            WHERE  qs.locked_by IS NOT NULL
              AND  qs.locked_until IS NOT NULL
              AND  ($1::text IS NULL OR qs.queue_key = $1)
              AND  NOT EXISTS (
                  SELECT 1
                  FROM   worker_jobs wj
                  WHERE  wj.status::text = 'RUNNING'
                    AND  wj.queue_key = qs.queue_key
                    AND  wj.current_worker_id = qs.locked_by
                    AND  wj.current_queue_slot = qs.slot
              )
            """,
            queue_key,
        )
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0
