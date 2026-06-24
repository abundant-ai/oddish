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

from typing import Any

from oddish.config import settings
from oddish.db import get_pool


__all__ = [
    "build_spawn_plan",
    "discover_active_worker_job_queue_keys",
    "get_worker_job_org_queue_counts",
    "select_job_function",
]


async def discover_active_worker_job_queue_keys() -> tuple[tuple[str, str], ...]:
    """``(queue_key, harbor_variant_id)`` pairs with pending / running rows.

    The Harbor variant is part of the effective dispatch key, so discovery
    surfaces it alongside the queue_key: the dispatcher spawns a worker per
    ``(queue_key, variant)`` (default + ephemeral on the default image, blessed
    ids on their own image). Single query across every kind, gated by
    ``available_after`` so scheduled-in-the-future rows don't wake the
    dispatcher early. The queue_key is normalized (raw + ``normalize_queue_key``
    forms, each paired with the variant).
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT queue_key, harbor_variant_id
        FROM   worker_jobs
        WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND  available_after <= NOW()
        """
    )

    discovered: set[tuple[str, str]] = set()
    for row in rows:
        raw_key = str(row["queue_key"]).strip().lower().replace(" ", "_")
        if not raw_key:
            continue
        variant = str(row["harbor_variant_id"] or "default")
        discovered.add((raw_key, variant))
        discovered.add((settings.normalize_queue_key(raw_key), variant))

    return tuple(sorted(discovered))


async def get_worker_job_org_queue_counts(
    queue_keys: tuple[str, ...],
) -> tuple[dict[tuple[str | None, str, str], int], dict[tuple[str, str], int]]:
    """Queued + RUNNING counts, both broken out by Harbor variant.

    Returns ``(queued_by_(org, queue_key, variant), running_by_(queue_key,
    variant))``. The variant is part of the effective dispatch key:

    * Queued work is scheduled **per org per queue_key per variant** so the
      planner can round-robin fairly across orgs and route each variant to its
      own worker Function.
    * RUNNING is counted **per (queue_key, variant)** so telemetry/routing see
      each lane, while the planner sums them per queue_key for the shared
      ``queue_slots`` capacity check (decision: variants share one queue_key
      concurrency pool for v1).

    Jobs with ``org_id IS NULL`` are grouped under a single ``None`` bucket so
    legacy / self-hosted rows without an org still flow through the planner.
    """
    if not queue_keys:
        return {}, {}

    pool = await get_pool()
    queued_by_org_queue: dict[tuple[str | None, str, str], int] = {}
    running_by_queue: dict[tuple[str, str], int] = {}

    queued_rows = await pool.fetch(
        """
        SELECT org_id, queue_key, harbor_variant_id, COUNT(*) AS queued
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text IN ('QUEUED', 'RETRYING')
          AND  available_after <= NOW()
        GROUP BY org_id, queue_key, harbor_variant_id
        """,
        list(queue_keys),
    )
    for row in queued_rows:
        count = int(row["queued"] or 0)
        if count <= 0:
            continue
        variant = str(row["harbor_variant_id"] or "default")
        queued_by_org_queue[(row["org_id"], row["queue_key"], variant)] = count

    running_rows = await pool.fetch(
        """
        SELECT queue_key, harbor_variant_id, COUNT(*) AS running
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text = 'RUNNING'
        GROUP BY queue_key, harbor_variant_id
        """,
        list(queue_keys),
    )
    for row in running_rows:
        variant = str(row["harbor_variant_id"] or "default")
        running_by_queue[(row["queue_key"], variant)] = int(row["running"] or 0)

    return queued_by_org_queue, running_by_queue


def select_job_function(
    unit: tuple[str, str],
    *,
    default_fn: Any,
    variant_fns: dict[str, Any],
) -> tuple[Any, dict[str, str]]:
    """Pick the worker Function + spawn kwargs for one ``(queue_key, variant)``.

    ``default`` and ``ephemeral`` run on the base ``process_single_job`` (the
    default image; ``ephemeral`` spawns an out-of-process child from there). A
    blessed ``<id>`` runs on its own image-bound Function from *variant_fns*. An
    id with no built image yet (registry/image drift) falls back to the base
    Function rather than being dropped. Every Function is passed
    ``harbor_variant_id`` so its claim is scoped to the right lane.
    """
    queue_key, variant = unit
    fn = variant_fns.get(variant, default_fn)
    return fn, {"queue_key": queue_key, "harbor_variant_id": variant}


def _org_sort_key(org_id: str | None) -> tuple[int, str]:
    """Deterministic ordering that keeps ``None`` (unowned rows) last."""
    return (1, "") if org_id is None else (0, org_id)


def build_spawn_plan(
    queued_by_org_queue: dict[tuple[str | None, str, str], int],
    running_by_queue: dict[tuple[str, str], int],
    concurrency_limits: dict[str, int],
    max_workers: int,
) -> list[tuple[str, str]]:
    """Decide which ``(queue_key, harbor_variant_id)`` workers to spawn.

    Two-level round-robin:

    1. **Outer (orgs)** — strict round-robin across ``org_id`` so no
       single org can consume the whole per-poll spawn budget just
       because they enqueue across more models than their neighbour.
    2. **Inner (``(queue_key, variant)`` units within an org)** —
       round-robin across that org's units using a per-org cursor.
       Heavier units keep getting spawns turn after turn because their
       queued count never drops to zero, but lighter units still receive
       at least one spawn per org-turn -- the "leeway" that prevents a
       small secondary unit from being completely starved.

    Per-queue_key capacity is ``limit - running`` where ``running`` SUMS
    across that queue_key's variants: for v1 the default and any variants
    of a queue_key share one ``queue_slots`` concurrency pool (the variant
    split is dispatch-only). Capacity is decremented across orgs and across
    variants of the same queue_key, so the global cap continues to dominate.
    """
    if max_workers <= 0 or not queued_by_org_queue:
        return []

    # Bucket queued work by org -> {(queue_key, variant): queued}.
    org_to_unit_queued: dict[str | None, dict[tuple[str, str], int]] = {}
    for (org_id, queue_key, variant), queued in queued_by_org_queue.items():
        if queued <= 0:
            continue
        org_to_unit_queued.setdefault(org_id, {})[(queue_key, variant)] = queued

    if not org_to_unit_queued:
        return []

    # Running counts SUM across variants for the shared per-queue_key cap.
    running_by_queue_key: dict[str, int] = {}
    for (queue_key, _variant), running in running_by_queue.items():
        running_by_queue_key[queue_key] = running_by_queue_key.get(queue_key, 0) + (
            running or 0
        )

    global_capacity: dict[str, int] = {}
    all_queue_keys = set(concurrency_limits.keys()) | {
        qk for bucket in org_to_unit_queued.values() for (qk, _v) in bucket
    }
    for queue_key in all_queue_keys:
        limit = concurrency_limits.get(queue_key, 0)
        running = running_by_queue_key.get(queue_key, 0)
        global_capacity[queue_key] = max(limit - running, 0)

    ordered_orgs = sorted(org_to_unit_queued.keys(), key=_org_sort_key)
    per_org_units: dict[str | None, list[tuple[str, str]]] = {
        org_id: sorted(org_to_unit_queued[org_id].keys()) for org_id in ordered_orgs
    }
    per_org_cursor: dict[str | None, int] = {org_id: 0 for org_id in ordered_orgs}

    spawn_plan: list[tuple[str, str]] = []
    while len(spawn_plan) < max_workers:
        progressed = False
        for org_id in ordered_orgs:
            if len(spawn_plan) >= max_workers:
                break
            units = per_org_units[org_id]
            if not units:
                continue

            # Advance the cursor at most one full cycle looking for a
            # (queue_key, variant) unit with both org-level queued work and
            # global (shared per-queue_key) capacity remaining. Stopping after
            # one cycle avoids a spin when none of this org's units are
            # spawnable right now (the outer while-loop breaks on no-progress).
            picked: tuple[str, str] | None = None
            for _ in range(len(units)):
                idx = per_org_cursor[org_id] % len(units)
                candidate = units[idx]
                per_org_cursor[org_id] = (per_org_cursor[org_id] + 1) % len(units)
                if (
                    org_to_unit_queued[org_id].get(candidate, 0) > 0
                    and global_capacity.get(candidate[0], 0) > 0
                ):
                    picked = candidate
                    break

            if picked is not None:
                spawn_plan.append(picked)
                org_to_unit_queued[org_id][picked] -= 1
                global_capacity[picked[0]] -= 1
                progressed = True

        if not progressed:
            break

    return spawn_plan
