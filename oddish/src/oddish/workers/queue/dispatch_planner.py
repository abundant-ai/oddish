from __future__ import annotations

from oddish.config import settings
from oddish.db import get_pool


async def discover_active_queue_keys() -> tuple[str, ...]:
    """Discover queue entrypoints that currently need dispatch capacity."""
    pool = await get_pool()

    # Trial queue keys (from the trials table directly)
    rows = await pool.fetch(
        """
        SELECT DISTINCT queue_key
        FROM trials
        WHERE status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
        """
    )
    discovered: set[str] = set()
    for row in rows:
        raw_key = str(row["queue_key"]).strip().lower().replace(" ", "_")
        if raw_key:
            discovered.add(raw_key)
            discovered.add(settings.normalize_queue_key(raw_key))

    # Add analysis queue key if there are pending analyses
    analysis_count = await pool.fetchval(
        "SELECT COUNT(*) FROM trials WHERE analysis_status::text = 'QUEUED'"
    )
    if analysis_count and analysis_count > 0:
        discovered.add(settings.normalize_queue_key(settings.get_analysis_queue_key()))

    # Add verdict queue key if there are pending verdicts
    verdict_count = await pool.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE verdict_status::text = 'QUEUED'"
    )
    if verdict_count and verdict_count > 0:
        discovered.add(settings.normalize_queue_key(settings.get_verdict_queue_key()))

    discovered.update(settings.get_known_queue_keys())
    if not discovered:
        discovered = {"default"}
    return tuple(sorted(discovered))


async def get_queue_counts(
    queue_keys: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    """Fetch queued/running counts per queue key from the trials table."""
    if not queue_keys:
        return {}

    pool = await get_pool()
    counts = {queue_key: {"queued": 0, "picked": 0} for queue_key in queue_keys}

    # Trial counts by queue_key
    rows = await pool.fetch(
        """
        SELECT
            queue_key,
            COUNT(*) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')) AS queued,
            COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running
        FROM trials
        WHERE queue_key = ANY($1)
        GROUP BY queue_key
        """,
        list(queue_keys),
    )
    for row in rows:
        qk = row["queue_key"]
        if qk in counts:
            counts[qk]["queued"] = int(row["queued"] or 0)
            counts[qk]["picked"] = int(row["running"] or 0)

    # Analysis counts
    analysis_key = settings.normalize_queue_key(settings.get_analysis_queue_key())
    if analysis_key in counts:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE analysis_status::text = 'QUEUED') AS queued,
                COUNT(*) FILTER (WHERE analysis_status::text = 'RUNNING') AS running
            FROM trials
            WHERE analysis_status IS NOT NULL
            """
        )
        if row:
            counts[analysis_key]["queued"] = int(row["queued"] or 0)
            counts[analysis_key]["picked"] = int(row["running"] or 0)

    # Verdict counts
    verdict_key = settings.normalize_queue_key(settings.get_verdict_queue_key())
    if verdict_key in counts:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE verdict_status::text = 'QUEUED') AS queued,
                COUNT(*) FILTER (WHERE verdict_status::text = 'RUNNING') AS running
            FROM tasks
            WHERE verdict_status IS NOT NULL
            """
        )
        if row:
            counts[verdict_key]["queued"] = int(row["queued"] or 0)
            counts[verdict_key]["picked"] = int(row["running"] or 0)

    return counts


def build_spawn_plan(
    queue_counts: dict[str, dict[str, int]],
    concurrency_limits: dict[str, int],
    max_workers: int,
) -> list[str]:
    """Decide which queue-specific workers to spawn this cycle."""
    queue_keys = sorted(set(queue_counts.keys()) | set(concurrency_limits.keys()))
    capacity_by_queue: dict[str, int] = {}
    for queue_key in queue_keys:
        queued = queue_counts.get(queue_key, {}).get("queued", 0)
        running = queue_counts.get(queue_key, {}).get("picked", 0)
        limit = concurrency_limits.get(queue_key, 0)
        capacity_by_queue[queue_key] = max(min(queued, limit - running), 0)

    total_capacity = sum(capacity_by_queue.values())
    if total_capacity <= 0 or max_workers <= 0:
        return []

    workers_to_spawn = min(total_capacity, max_workers)
    spawn_plan: list[str] = []
    while len(spawn_plan) < workers_to_spawn:
        progressed = False
        for queue_key in queue_keys:
            if len(spawn_plan) >= workers_to_spawn:
                break
            if capacity_by_queue.get(queue_key, 0) > 0:
                spawn_plan.append(queue_key)
                capacity_by_queue[queue_key] -= 1
                progressed = True
        if not progressed:
            break
    return spawn_plan
