"""One job cohort predicate shared by dispatch counts and atomic claims.

The MD5 bucket is a deterministic split of job IDs, not a security primitive.
Retries keep their cohort. No Modal dependency: self-hosted callers opt out.
"""

import asyncpg

from oddish.workers.queue.worker_job_dispatcher import QueueDemandKey

COHORT_SQL = """COALESCE((
    wj.kind::text = 'TRIAL' AND wj.subject_table = 'trials'
    AND tr.kind = 'agent' AND NOT tr.is_probe
    AND wj.priority <= 0 AND wj.harbor_variant_id = 'default'
    AND wj.execution_lane = 'default'
    AND tr.deleted_at IS NULL AND tk.deleted_at IS NULL
    AND ('x' || substr(md5(wj.id), 1, 8))::bit(32)::bigint
        < {fraction}::double precision * 4294967296
), false)"""


async def load_rollout(
    connection: asyncpg.Connection | asyncpg.Pool,
    *,
    configuration: str,
    lock: bool = False,
) -> tuple[float, int]:
    # Claims take a shared row lock only until the claim commits. Setting zero
    # waits for those short claims, then no subsequent claim can see the old value.
    row = await connection.fetchrow(
        "SELECT fraction, max_workers, configuration FROM worker_resource_rollout "
        "WHERE id = 1" + (" FOR SHARE" if lock else "")
    )
    if row is None or row["configuration"] != configuration or row["max_workers"] <= 0:
        return 0.0, 0
    return float(row["fraction"]), int(row["max_workers"])


async def candidate_queue_counts(
    connection: asyncpg.Connection | asyncpg.Pool, fraction: float
) -> dict[QueueDemandKey, int]:
    if fraction <= 0:
        return {}
    rows = await connection.fetch(
        """
        SELECT wj.org_id, wj.queue_key, COUNT(*) AS queued
        FROM worker_jobs wj
        JOIN trials tr ON wj.subject_id = tr.id
        JOIN tasks tk ON tr.task_id = tk.id
        WHERE wj.status::text IN ('QUEUED', 'RETRYING')
          AND wj.available_after <= NOW() AND """
        + COHORT_SQL.format(fraction="$1")
        + " GROUP BY wj.org_id, wj.queue_key",
        fraction,
    )
    return {
        (row["org_id"], row["queue_key"], "default", "default", False): int(
            row["queued"]
        )
        for row in rows
    }
