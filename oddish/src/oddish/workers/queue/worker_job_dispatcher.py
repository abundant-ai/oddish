"""Dispatcher helpers that read from the unified `worker_jobs` table.

Replaces the three-way union the legacy dispatcher used to do over
trials / trial-analyses / task-verdicts. The whole point of the
unified queue is that we stop growing the dispatcher every time a new
kind is added -- a new ``WorkerJobKind`` just shows up in the
``DISTINCT queue_key`` / ``GROUP BY queue_key`` rows here without any
change to this module.

Fairness model
--------------
``build_spawn_plan`` applies **org-first** fair-share so a single org
with queued work across many models cannot monopolise the per-poll
spawn budget. Within one org the planner round-robins across that
org's queue_keys -- heavier models naturally keep receiving spawns
(they never run dry), but lighter models still get at least one spawn
per org-turn so they don't starve entirely. Starvation between a
single org's models is tolerated; starvation between orgs is not.

Per-queue_key global concurrency caps (``queue_slots`` leases) are
still enforced on top: even if an org has budget left, we won't spawn
beyond ``limit - running`` for that queue_key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from oddish.config import settings
from oddish.db import get_pool


__all__ = [
    "build_spawn_plan",
    "discover_active_worker_job_queue_keys",
    "get_worker_job_org_queue_counts",
    "next_retry_wake_at",
    "plan_dispatch_cycle",
    "run_dispatch_cycle",
]

Spawner = Callable[[str], Awaitable[None]]


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


async def get_worker_job_org_queue_counts(
    queue_keys: tuple[str, ...],
) -> tuple[dict[tuple[str | None, str], int], dict[str, int]]:
    """Per-(org_id, queue_key) queued counts plus per-queue_key RUNNING counts.

    Two shapes because the dispatcher needs different axes:

    * Queued work is scheduled **per org per queue_key** so we can
      round-robin fairly across orgs.
    * RUNNING count is a global-per-queue_key capacity check --
      ``queue_slots`` caps are enforced across orgs, not per-org.

    Jobs with ``org_id IS NULL`` are grouped under a single ``None``
    bucket so legacy / self-hosted rows without an org still flow
    through the planner instead of being silently dropped.
    """
    if not queue_keys:
        return {}, {}

    pool = await get_pool()
    queued_by_org_queue: dict[tuple[str | None, str], int] = {}
    running_by_queue: dict[str, int] = {queue_key: 0 for queue_key in queue_keys}

    queued_rows = await pool.fetch(
        """
        SELECT org_id, queue_key, COUNT(*) AS queued
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text IN ('QUEUED', 'RETRYING')
          AND  available_after <= NOW()
        GROUP BY org_id, queue_key
        """,
        list(queue_keys),
    )
    for row in queued_rows:
        count = int(row["queued"] or 0)
        if count <= 0:
            continue
        queued_by_org_queue[(row["org_id"], row["queue_key"])] = count

    running_rows = await pool.fetch(
        """
        SELECT queue_key, COUNT(*) AS running
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text = 'RUNNING'
        GROUP BY queue_key
        """,
        list(queue_keys),
    )
    for row in running_rows:
        running_by_queue[row["queue_key"]] = int(row["running"] or 0)

    return queued_by_org_queue, running_by_queue


def _org_sort_key(org_id: str | None) -> tuple[int, str]:
    """Deterministic ordering that keeps ``None`` (unowned rows) last."""
    return (1, "") if org_id is None else (0, org_id)


def build_spawn_plan(
    queued_by_org_queue: dict[tuple[str | None, str], int],
    running_by_queue: dict[str, int],
    concurrency_limits: dict[str, int],
    max_workers: int,
) -> list[str]:
    """Decide which queue-specific workers to spawn this cycle.

    Two-level round-robin:

    1. **Outer (orgs)** — strict round-robin across ``org_id`` so no
       single org can consume the whole per-poll spawn budget just
       because they enqueue across more models than their neighbour.
    2. **Inner (queue_keys within an org)** — round-robin across that
       org's queue_keys using a per-org cursor. Heavier models keep
       getting spawns turn after turn because their queued count never
       drops to zero, but lighter models still receive at least one
       spawn per org-turn -- the "leeway" that prevents a small
       secondary model from being completely starved behind a large
       primary one.

    Per-queue_key capacity is ``limit - running`` and is decremented
    across orgs -- the global ``queue_slots`` concurrency cap continues
    to dominate, so one org cannot crowd out another at the claim
    level just because the planner tried to spawn more workers for it.
    """
    if max_workers <= 0 or not queued_by_org_queue:
        return []

    # Bucket queued work by org and compute remaining global capacity
    # per queue_key once, up front; both get mutated as we allocate.
    org_to_qk_queued: dict[str | None, dict[str, int]] = {}
    for (org_id, queue_key), queued in queued_by_org_queue.items():
        if queued <= 0:
            continue
        org_to_qk_queued.setdefault(org_id, {})[queue_key] = queued

    if not org_to_qk_queued:
        return []

    global_capacity: dict[str, int] = {}
    all_queue_keys = set(concurrency_limits.keys()) | {
        qk for bucket in org_to_qk_queued.values() for qk in bucket
    }
    for queue_key in all_queue_keys:
        limit = concurrency_limits.get(queue_key, 0)
        running = running_by_queue.get(queue_key, 0)
        global_capacity[queue_key] = max(limit - running, 0)

    ordered_orgs = sorted(org_to_qk_queued.keys(), key=_org_sort_key)
    per_org_qks: dict[str | None, list[str]] = {
        org_id: sorted(org_to_qk_queued[org_id].keys()) for org_id in ordered_orgs
    }
    per_org_cursor: dict[str | None, int] = {org_id: 0 for org_id in ordered_orgs}

    spawn_plan: list[str] = []
    while len(spawn_plan) < max_workers:
        progressed = False
        for org_id in ordered_orgs:
            if len(spawn_plan) >= max_workers:
                break
            qks = per_org_qks[org_id]
            if not qks:
                continue

            # Advance the cursor at most one full cycle looking for a
            # queue_key with both org-level queued work and global
            # capacity remaining. Stopping after one cycle avoids a
            # spin when none of this org's queues are spawnable right
            # now (the outer while-loop will break on no-progress).
            picked: str | None = None
            for _ in range(len(qks)):
                idx = per_org_cursor[org_id] % len(qks)
                candidate = qks[idx]
                per_org_cursor[org_id] = (per_org_cursor[org_id] + 1) % len(qks)
                if (
                    org_to_qk_queued[org_id].get(candidate, 0) > 0
                    and global_capacity.get(candidate, 0) > 0
                ):
                    picked = candidate
                    break

            if picked is not None:
                spawn_plan.append(picked)
                org_to_qk_queued[org_id][picked] -= 1
                global_capacity[picked] -= 1
                progressed = True

        if not progressed:
            break

    return spawn_plan


async def plan_dispatch_cycle(
    max_workers: int,
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """Discover queue state and produce a spawn plan.

    Returned tuple is ``(spawn_plan, queued_by_queue, running_by_queue)``;
    the two count maps are aggregated per-queue_key for operator logging.
    """
    if max_workers <= 0:
        return [], {}, {}

    queue_keys = await discover_active_worker_job_queue_keys()
    if not queue_keys:
        return [], {}, {}

    queued_by_org_queue, running_by_queue = await get_worker_job_org_queue_counts(
        queue_keys
    )
    concurrency_limits = {
        queue_key: settings.get_model_concurrency(queue_key) for queue_key in queue_keys
    }
    spawn_plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue=running_by_queue,
        concurrency_limits=concurrency_limits,
        max_workers=max_workers,
    )

    queued_by_queue: dict[str, int] = {}
    for (_org_id, queue_key), queued in queued_by_org_queue.items():
        queued_by_queue[queue_key] = queued_by_queue.get(queue_key, 0) + queued

    return spawn_plan, queued_by_queue, running_by_queue


async def run_dispatch_cycle(
    spawner: Spawner,
    *,
    max_workers: int,
) -> list[str]:
    """Plan a dispatch cycle and ``await spawner(queue_key)`` for each entry.

    Returns the spawn plan that was executed so callers can log it. The
    spawner is invoked sequentially via ``await``; callers that want
    parallel spawn fan-out should pass a spawner that schedules without
    blocking (e.g. wrap ``modal_function.spawn.aio`` in ``asyncio.gather``
    at the call site).
    """
    spawn_plan, _, _ = await plan_dispatch_cycle(max_workers=max_workers)
    for queue_key in spawn_plan:
        await spawner(queue_key)
    return spawn_plan


async def next_retry_wake_at() -> datetime | None:
    """Earliest ``available_after`` for a row not yet runnable.

    The reactor uses this to compute how long to sleep when no notify is
    pending: ``min(available_after) - NOW()`` is the longest it can wait
    before a retry must be picked up, regardless of whether any NOTIFY
    fires before then.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT MIN(available_after) AS wake_at
        FROM   worker_jobs
        WHERE  status::text IN ('QUEUED', 'RETRYING')
          AND  available_after > NOW()
        """
    )
    if row is None:
        return None
    wake_at = row["wake_at"]
    if wake_at is None:
        return None
    return wake_at
