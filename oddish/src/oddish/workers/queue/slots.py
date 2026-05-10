import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from oddish.db.connection import get_pool

# Burst-tolerance for slot bookkeeping. Slot ops are the very first thing
# every worker container does, so when Modal scales out many containers at
# once they can hammer Supavisor's session-mode connection limit
# simultaneously and bounce off with `MaxClientsInSessionMode`. These
# numbers cap the wait at about 30s before bubbling the error.
_CONNECT_RETRY_ATTEMPTS = 6
_CONNECT_RETRY_BASE_DELAY = 0.5  # seconds
_CONNECT_RETRY_MAX_DELAY = 8.0


def _is_transient_connect_error(exc: BaseException) -> bool:
    """Pool exhaustion / transient network errors that retrying may resolve."""
    if isinstance(exc, asyncpg.exceptions.InternalServerError):
        # Supavisor surfaces session-mode pool exhaustion as
        # `MaxClientsInSessionMode`; treat it as transient.
        return "MaxClientsInSessionMode" in str(exc)
    if isinstance(
        exc,
        (
            asyncpg.exceptions.TooManyConnectionsError,
            asyncpg.exceptions.CannotConnectNowError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        ),
    ):
        return True
    return False


@asynccontextmanager
async def _slot_connection() -> AsyncIterator[asyncpg.Connection]:
    # Acquire from the shared asyncpg pool instead of dialing a brand-new
    # connection on every slot op. With ``ODDISH_ASYNCPG_POOL_MAX_SIZE=1``
    # in Modal, this means one persistent connection per worker container
    # gets reused across slot acquire/release/cleanup calls — no extra
    # session-mode socket churn against Supavisor, and bursty cold starts
    # no longer race to open N parallel sockets only to close them again.
    pool = await get_pool()
    last_exc: BaseException | None = None
    for attempt in range(_CONNECT_RETRY_ATTEMPTS):
        try:
            async with pool.acquire() as conn:
                yield conn
                return
        except BaseException as e:  # noqa: BLE001 — narrowed below
            last_exc = e
            if not _is_transient_connect_error(e):
                raise
            if attempt == _CONNECT_RETRY_ATTEMPTS - 1:
                break
            # Exponential backoff with jitter so a thundering herd of
            # workers doesn't retry in lockstep.
            delay = min(
                _CONNECT_RETRY_MAX_DELAY,
                _CONNECT_RETRY_BASE_DELAY * (2**attempt),
            )
            delay *= 0.5 + random.random()
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


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
