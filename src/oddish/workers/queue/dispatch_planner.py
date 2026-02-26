from __future__ import annotations

from oddish.config import settings
from oddish.db import get_pool


async def discover_active_queue_keys() -> tuple[str, ...]:
    """Discover queue entrypoints that currently need dispatch capacity."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT entrypoint
        FROM pgqueuer
        WHERE status IN ('queued', 'picked')
        """
    )
    discovered: set[str] = set()
    for row in rows:
        entrypoint = row.get("entrypoint")
        if not entrypoint:
            continue
        raw_key = str(entrypoint).strip().lower().replace(" ", "_")
        if not raw_key:
            continue
        # Keep raw keys so legacy queued jobs still drain, and add canonical
        # keys so new writes converge to a single entrypoint.
        discovered.add(raw_key)
        discovered.add(settings.normalize_queue_key(raw_key))
    discovered.update(settings.get_known_queue_keys())
    if not discovered:
        discovered = {"default"}
    return tuple(sorted(discovered))


async def get_queue_counts(
    queue_keys: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    """Fetch queued/picked counts per queue entrypoint."""
    if not queue_keys:
        return {}

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT
            entrypoint,
            status::text AS status,
            COUNT(*) AS count
        FROM pgqueuer
        WHERE entrypoint = ANY($1)
          AND status IN ('queued', 'picked')
        GROUP BY entrypoint, status
        """,
        list(queue_keys),
    )

    counts = {queue_key: {"queued": 0, "picked": 0} for queue_key in queue_keys}
    for row in rows:
        entrypoint = row.get("entrypoint")
        status = row.get("status")
        if entrypoint in counts and status in counts[entrypoint]:
            counts[entrypoint][status] = int(row.get("count", 0))
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
