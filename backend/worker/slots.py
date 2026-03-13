from oddish.db import get_pool


async def ensure_queue_slots(queue_key: str, limit: int) -> None:
    """Ensure queue slot rows exist up to the configured limit."""
    if limit <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
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
    pool = await get_pool()
    async with pool.acquire() as conn:
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
    pool = await get_pool()
    async with pool.acquire() as conn:
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
    pool = await get_pool()
    async with pool.acquire() as conn:
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
