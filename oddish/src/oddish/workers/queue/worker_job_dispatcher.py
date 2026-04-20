"""Dispatcher helpers that read from the unified `worker_jobs` table.

Replaces the three-way union the legacy dispatcher used to do over
trials / trial-analyses / task-verdicts. The whole point of the
unified queue is that we stop growing the dispatcher every time a new
kind is added -- a new ``WorkerJobKind`` just shows up in the
``DISTINCT queue_key`` / ``GROUP BY queue_key`` rows here without any
change to this module.
"""

from __future__ import annotations

from oddish.config import settings
from oddish.db import get_pool


__all__ = [
    "build_spawn_plan",
    "discover_active_worker_job_queue_keys",
    "get_worker_job_queue_counts",
]


async def discover_active_worker_job_queue_keys() -> tuple[str, ...]:
    """Queue keys with pending / running `worker_jobs` rows.

    Single query across every kind, gated by ``available_after`` so
    scheduled-in-the-future rows don't wake up the dispatcher early.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT queue_key
        FROM   worker_jobs
        WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND  available_after <= NOW()
        """
    )

    discovered: set[str] = set()
    for row in rows:
        raw_key = str(row["queue_key"]).strip().lower().replace(" ", "_")
        if not raw_key:
            continue
        discovered.add(raw_key)
        discovered.add(settings.normalize_queue_key(raw_key))

    return tuple(sorted(discovered))


async def get_worker_job_queue_counts(
    queue_keys: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    """Per-queue-key queued/running counts from ``worker_jobs``.

    Returns ``{"queued": int, "picked": int}`` per queue key (the
    "picked" naming is preserved from the legacy planner shape that
    callers already consume).
    """
    if not queue_keys:
        return {}

    pool = await get_pool()
    counts = {queue_key: {"queued": 0, "picked": 0} for queue_key in queue_keys}

    rows = await pool.fetch(
        """
        SELECT
            queue_key,
            COUNT(*) FILTER (
                WHERE status::text IN ('QUEUED', 'RETRYING')
                  AND available_after <= NOW()
            ) AS queued,
            COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
        GROUP BY queue_key
        """,
        list(queue_keys),
    )
    for row in rows:
        qk = row["queue_key"]
        if qk in counts:
            counts[qk]["queued"] = int(row["queued"] or 0)
            counts[qk]["picked"] = int(row["running"] or 0)

    return counts


def build_spawn_plan(
    queue_counts: dict[str, dict[str, int]],
    concurrency_limits: dict[str, int],
    max_workers: int,
) -> list[str]:
    """Decide which queue-specific workers to spawn this cycle.

    Round-robin across queue keys so no single queue monopolises the
    per-tick spawn budget. Each queue key's capacity is
    ``min(queued, limit - running)`` -- the min of "work waiting" and
    "headroom under the concurrency cap".
    """
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
