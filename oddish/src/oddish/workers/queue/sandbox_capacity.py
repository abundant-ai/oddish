"""Provider-wide sandbox capacity leases."""

from __future__ import annotations

from oddish.db import get_pool

SANDBOX_CAPACITY_LEASE_SECONDS = 120


async def acquire_sandbox_capacity_lease(
    *, provider: str, limit: int, worker_id: str, lease_seconds: int
) -> int | None:
    if limit <= 0:
        return None
    pool = await get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO sandbox_capacity_leases (provider, slot)
                SELECT $1, slot
                FROM generate_series(0, $2 - 1) AS slot
                ON CONFLICT (provider, slot) DO NOTHING
                """,
                provider,
                limit,
            )
            row = await connection.fetchrow(
                """
                WITH available AS (
                    SELECT provider, slot
                    FROM sandbox_capacity_leases
                    WHERE provider = $1
                      AND slot < $2
                      AND (
                          locked_by IS NULL
                          OR locked_until IS NULL
                          OR (
                              locked_until <= NOW()
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM worker_jobs AS wj
                                  WHERE wj.id = sandbox_capacity_leases.worker_job_id
                                    AND wj.status::text = 'RUNNING'
                                    AND wj.current_worker_id = sandbox_capacity_leases.locked_by
                              )
                          )
                      )
                    ORDER BY slot
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE sandbox_capacity_leases AS lease
                SET locked_by = $3,
                    worker_job_id = NULL,
                    locked_at = NOW(),
                    locked_until = NOW() + make_interval(secs => $4)
                FROM available
                WHERE lease.provider = available.provider
                  AND lease.slot = available.slot
                RETURNING lease.slot
                """,
                provider,
                limit,
                worker_id,
                lease_seconds,
            )
    return int(row["slot"]) if row is not None else None


async def release_sandbox_capacity_lease(
    *, provider: str, slot: int, worker_id: str
) -> bool:
    pool = await get_pool()
    command = await pool.execute(
        """
        UPDATE sandbox_capacity_leases
        SET locked_by = NULL,
            worker_job_id = NULL,
            locked_at = NULL,
            locked_until = NULL
        WHERE provider = $1
          AND slot = $2
          AND locked_by = $3
        """,
        provider,
        slot,
        worker_id,
    )
    return bool(command.endswith(" 1"))


async def count_held_sandbox_capacity_leases(*, provider: str) -> int:
    """Count every live capacity lease for a provider."""
    pool = await get_pool()
    count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM sandbox_capacity_leases
        WHERE provider = $1
          AND locked_by IS NOT NULL
          AND (
              locked_until > NOW()
              OR EXISTS (
                  SELECT 1
                  FROM worker_jobs AS wj
                  WHERE wj.id = sandbox_capacity_leases.worker_job_id
                    AND wj.status::text = 'RUNNING'
                    AND wj.current_worker_id = sandbox_capacity_leases.locked_by
              )
          )
        """,
        provider,
    )
    return int(count or 0)


async def cleanup_sandbox_capacity_leases(*, grace_seconds: int = 120) -> int:
    """Release expired leases and vanished-worker leases after a short grace."""
    pool = await get_pool()
    command = await pool.execute(
        """
        UPDATE sandbox_capacity_leases AS lease
        SET locked_by = NULL,
            worker_job_id = NULL,
            locked_at = NULL,
            locked_until = NULL
        WHERE lease.locked_by IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM worker_jobs AS wj
              WHERE wj.id = lease.worker_job_id
                AND wj.status::text = 'RUNNING'
                AND wj.current_worker_id = lease.locked_by
          )
          AND (
              lease.locked_until <= NOW()
              OR lease.locked_at <= NOW() - make_interval(secs => $1)
          )
        """,
        grace_seconds,
    )
    return int(command.rsplit(" ", 1)[-1])


__all__ = [
    "SANDBOX_CAPACITY_LEASE_SECONDS",
    "acquire_sandbox_capacity_lease",
    "cleanup_sandbox_capacity_leases",
    "count_held_sandbox_capacity_leases",
    "release_sandbox_capacity_lease",
]
