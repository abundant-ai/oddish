"""Dispatcher helpers that read from the unified `worker_jobs` table.

Replaces the three-way union the legacy dispatcher used to do over
trials / trial-analyses / task-verdicts. Active kinds share the same
``DISTINCT queue_key`` / ``GROUP BY queue_key`` queries; historical enum-only
kinds are excluded by the shared ``ACTIVE_WORKER_JOB_KINDS`` definition.

The planner rotates organizations, then gives positive-priority analysis jobs
three turns for each ordinary turn within an organization. Hosted dispatch
persists those turns and reserves the existing queue_slots before calling Modal.
All priority classes, organizations, variants, and lanes share the model cap.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, NamedTuple

from oddish.config import settings
from oddish.db import ACTIVE_WORKER_JOB_KINDS, get_pool


# Positive priorities include QA, audit, QA-eval and summarize trials.
QueueDemandKey = tuple[str | None, str, str, str, bool]


class DispatchUnit(NamedTuple):
    queue_key: str
    harbor_variant_id: str
    execution_lane: str
    priority_class: bool
    org_id: str | None


_ACTIVE_KIND_VALUES = tuple(kind.value for kind in ACTIVE_WORKER_JOB_KINDS)


__all__ = [
    "build_spawn_plan",
    "discover_active_worker_job_queue_keys",
    "fetch_running_worker_handles",
    "get_worker_job_org_queue_counts",
    "select_job_function",
    "stamp_dispatch_stage",
]


async def fetch_running_worker_handles() -> list[tuple[str, str]]:
    """``(queue_key, durable handle id)`` for RUNNING jobs with a persisted handle.

    Source for control-plane-restart reattach (``Dispatcher.recover``). Today the
    only persisted worker-container handle is ``modal_function_call_id`` (the
    Modal worker self-reports it at claim); backends whose handles are not durable
    leave it NULL and are skipped here, falling back to lease expiry + re-claim
    (design spec §14.3).
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT queue_key, modal_function_call_id
        FROM   worker_jobs
        WHERE  status::text = 'RUNNING'
          AND  modal_function_call_id IS NOT NULL
        """
    )
    return [
        (str(row["queue_key"]), str(row["modal_function_call_id"]))
        for row in rows
        if row["modal_function_call_id"]
    ]


async def stamp_dispatch_stage(
    spawned_keys: list[str],
    why_waiting: dict[str, str],
) -> None:
    """Record per-queue dispatch observability on waiting ``worker_jobs`` rows.

    * ``admission_reason`` for queue_keys still waiting (the §12 why-waiting
      field), so every Stage 2-5 wait has a named, queryable reason -- including
      the rows of a *partially* served key that got no worker this cycle.
    * ``spawned_at = NOW()`` for the rows we just dispatched a worker for. The
      spawn plan can schedule *several* workers for one ``queue_key`` in a
      cycle, so ``spawned_keys`` carries that per-key multiplicity: stamp only
      the oldest N still-unstamped rows per queue_key (N = workers spawned for
      it), oldest-first (``priority DESC, created_at ASC``) to match claim order,
      and clear any stale waiting-reason on just those rows.

    Reasons are written first and the spawned rows are stamped (and their reason
    cleared) second, so a partially served key ends with each row in exactly one
    state -- ``spawned_at`` set for the served rows, an ``admission_reason`` for
    the rest -- instead of the two writes fighting over the same rows.

    Best-effort telemetry: callers run it after ``Dispatcher.spawn`` and do not
    depend on its result.
    """
    pool = await get_pool()

    for queue_key, reason in why_waiting.items():
        await pool.execute(
            """
            UPDATE worker_jobs
            SET    admission_reason = $2
            WHERE  queue_key = $1
              AND  status::text IN ('QUEUED', 'RETRYING')
              AND  kind::text = ANY($3::text[])
            """,
            queue_key,
            reason,
            _ACTIVE_KIND_VALUES,
        )

    # Count workers spawned per queue_key (the spawn plan's multiplicity) so we
    # stamp exactly that many rows -- stamping every QUEUED/RETRYING row would
    # mark rows no worker was dispatched for and erase their waiting reason.
    for queue_key, spawned in Counter(spawned_keys).items():
        if spawned <= 0:
            continue
        await pool.execute(
            """
            WITH to_stamp AS (
                SELECT id
                FROM   worker_jobs
                WHERE  queue_key = $1
                  AND  status::text IN ('QUEUED', 'RETRYING')
                  AND  kind::text = ANY($3::text[])
                  AND  spawned_at IS NULL
                ORDER  BY priority DESC, created_at ASC
                LIMIT  $2
            )
            UPDATE worker_jobs
            SET    spawned_at = NOW(),
                   admission_reason = NULL
            WHERE  id IN (SELECT id FROM to_stamp)
            """,
            queue_key,
            spawned,
            _ACTIVE_KIND_VALUES,
        )


async def discover_active_worker_job_queue_keys() -> tuple[tuple[str, str, str], ...]:
    """``(queue_key, harbor_variant_id, execution_lane)`` active units.

    The Harbor variant is part of the effective dispatch key, so discovery
    surfaces it alongside the queue_key: the dispatcher spawns a worker per
    ``(queue_key, variant)`` (default + ephemeral on the default image, blessed
    ids on their own image). Single query across every active kind, gated by
    ``available_after`` so scheduled-in-the-future rows don't wake the
    dispatcher early. The queue_key is normalized (raw + ``normalize_queue_key``
    forms, each paired with the variant).
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT queue_key, harbor_variant_id, execution_lane
        FROM   worker_jobs
        WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND  available_after <= NOW()
          AND  kind::text = ANY($1::text[])
        """,
        _ACTIVE_KIND_VALUES,
    )

    discovered: set[tuple[str, str, str]] = set()
    for row in rows:
        raw_key = str(row["queue_key"]).strip().lower().replace(" ", "_")
        if not raw_key:
            continue
        variant = str(row["harbor_variant_id"] or "default")
        lane = str(row["execution_lane"] or "default")
        discovered.add((raw_key, variant, lane))
        discovered.add((settings.normalize_queue_key(raw_key), variant, lane))

    return tuple(sorted(discovered))


async def get_worker_job_org_queue_counts(
    queue_keys: tuple[str, ...],
) -> tuple[
    dict[QueueDemandKey, int],
    dict[tuple[str, str, str], int],
]:
    """Queued + RUNNING counts by Harbor variant and execution lane.

    Returns ``(queued_by_(org, queue_key, variant, lane, priority_class),
    running_by_(queue_key, variant, lane))``. Variant and lane are both part of
    the effective dispatch key:

    * Queued work is scheduled **per org per queue_key per variant** so the
      planner can round-robin fairly across orgs and route each credential class
      to the correct worker Function.
    * RUNNING is counted **per (queue_key, variant, lane)** while the planner
      sums them per queue_key for the shared
      ``queue_slots`` capacity check (decision: variants share one queue_key
      concurrency pool for v1).

    Jobs with ``org_id IS NULL`` are grouped under a single ``None`` bucket so
    legacy / self-hosted rows without an org still flow through the planner.
    """
    if not queue_keys:
        return {}, {}

    pool = await get_pool()
    queued_by_org_queue: dict[QueueDemandKey, int] = {}
    running_by_queue: dict[tuple[str, str, str], int] = {}

    queued_rows = await pool.fetch(
        """
        SELECT org_id, queue_key, harbor_variant_id, execution_lane,
               (priority > 0) AS priority_class, COUNT(*) AS queued
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text IN ('QUEUED', 'RETRYING')
          AND  available_after <= NOW()
          AND  kind::text = ANY($2::text[])
        GROUP BY org_id, queue_key, harbor_variant_id, execution_lane, (priority > 0)
        """,
        list(queue_keys),
        _ACTIVE_KIND_VALUES,
    )
    for row in queued_rows:
        count = int(row["queued"] or 0)
        if count <= 0:
            continue
        variant = str(row["harbor_variant_id"] or "default")
        lane = str(row["execution_lane"] or "default")
        queued_by_org_queue[
            (row["org_id"], row["queue_key"], variant, lane, row["priority_class"])
        ] = count

    running_rows = await pool.fetch(
        """
        SELECT queue_key, harbor_variant_id, execution_lane, COUNT(*) AS running
        FROM   worker_jobs
        WHERE  queue_key = ANY($1)
          AND  status::text = 'RUNNING'
          AND  kind::text = ANY($2::text[])
        GROUP BY queue_key, harbor_variant_id, execution_lane
        """,
        list(queue_keys),
        _ACTIVE_KIND_VALUES,
    )
    for row in running_rows:
        variant = str(row["harbor_variant_id"] or "default")
        lane = str(row["execution_lane"] or "default")
        running_by_queue[(row["queue_key"], variant, lane)] = int(row["running"] or 0)

    return queued_by_org_queue, running_by_queue


def select_job_function(
    unit: DispatchUnit,
    *,
    default_fn: Any,
    variant_fns: dict[str, Any],
    ec2_fn: Any | None = None,
    ec2_variant_fns: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Pick the worker Function and retain the allocated organization/class.

    The execution lane chooses the credential topology first; Harbor variant
    then chooses the image within that topology.
    """
    queue_key, variant, lane, priority_class, org_id = unit
    if lane == "ec2_trial":
        if ec2_fn is None:
            raise RuntimeError("EC2 dispatch unit has no EC2 worker Function")
        fn = (ec2_variant_fns or {}).get(variant, ec2_fn)
    else:
        fn = variant_fns.get(variant, default_fn)
    return fn, {
        "queue_key": queue_key,
        "harbor_variant_id": variant,
        "execution_lane": lane,
        "priority_class": priority_class,
        "org_id": org_id,
    }


def _org_sort_key(org_id: str | None) -> tuple[int, str]:
    """Deterministic ordering that keeps ``None`` (unowned rows) last."""
    return (1, "") if org_id is None else (0, org_id)


def build_spawn_plan(
    queued_by_org_queue: dict[QueueDemandKey, int],
    running_by_queue: dict[tuple[str, str, str], int],
    concurrency_limits: dict[str, int],
    max_workers: int,
    held_by_queue_key: dict[str, int] | None = None,
    capacity_limits_by_lane: dict[str, int] | None = None,
    held_by_lane: dict[str, int] | None = None,
    fairness_cursors: dict[str, int] | None = None,
) -> list[DispatchUnit]:
    """Allocate org turns round-robin, with a 3:1 analysis/ordinary preference.

    Each org's fourth turn prefers priority <= 0; the other three prefer > 0.
    If that class has no eligible capacity, the other class borrows the turn.
    Units within a class rotate too. Hosted dispatch persists the cursors with
    its launch reservations, including polls that can allocate only one worker.
    Claimers retain priority/user/FIFO ordering within the selected org/class.
    All classes and variants share max(RUNNING, held) model capacity.
    """
    if max_workers <= 0 or not queued_by_org_queue:
        return []

    # Bucket queued work by org -> {(queue_key, variant, lane): queued}.
    org_to_unit_queued: dict[str | None, dict[tuple[str, str, str, bool], int]] = {}
    for (
        org_id,
        queue_key,
        variant,
        lane,
        priority,
    ), queued in queued_by_org_queue.items():
        if queued <= 0:
            continue
        org_to_unit_queued.setdefault(org_id, {})[
            (queue_key, variant, lane, priority)
        ] = queued

    if not org_to_unit_queued:
        return []

    # Running counts SUM across variants for the shared per-queue_key cap.
    running_by_queue_key: dict[str, int] = {}
    for (queue_key, _variant, _lane), running in running_by_queue.items():
        running_by_queue_key[queue_key] = running_by_queue_key.get(queue_key, 0) + (
            running or 0
        )

    # In-flight per queue_key is max(RUNNING, held queue_slots leases). A lease is
    # acquired at spawn/claim, before the job shows RUNNING, so when the caller
    # supplies held-lease counts they include both launches and running workers.
    held = held_by_queue_key or {}

    global_capacity: dict[str, int] = {}
    all_queue_keys = set(concurrency_limits.keys()) | {
        qk
        for bucket in org_to_unit_queued.values()
        for (qk, _v, _lane, _priority) in bucket
    }
    for queue_key in all_queue_keys:
        limit = concurrency_limits.get(queue_key, 0)
        in_flight = max(running_by_queue_key.get(queue_key, 0), held.get(queue_key, 0))
        global_capacity[queue_key] = max(limit - in_flight, 0)

    running_by_lane: dict[str, int] = {}
    for (_queue_key, _variant, lane), running in running_by_queue.items():
        running_by_lane[lane] = running_by_lane.get(lane, 0) + (running or 0)
    lane_capacity: dict[str, int] = {}
    for lane, limit in (capacity_limits_by_lane or {}).items():
        in_flight = max(running_by_lane.get(lane, 0), (held_by_lane or {}).get(lane, 0))
        lane_capacity[lane] = max(limit - in_flight, 0)

    cursors = fairness_cursors if fairness_cursors is not None else {}
    ordered_orgs = sorted(org_to_unit_queued, key=_org_sort_key)
    offset = cursors.get("org_offset", 0) % len(ordered_orgs)
    orgs = ordered_orgs[offset:] + ordered_orgs[:offset]
    spawn_plan: list[DispatchUnit] = []
    while len(spawn_plan) < max_workers:
        progressed = False
        for org_id in orgs:
            if len(spawn_plan) >= max_workers:
                break
            cursor_key = json.dumps(org_id)
            turn = cursors.get(cursor_key, 0)
            preferred = turn % 4 != 3
            eligible = sorted(
                unit
                for unit, queued in org_to_unit_queued[org_id].items()
                if queued > 0
                and global_capacity.get(unit[0], 0) > 0
                and lane_capacity.get(unit[2], 1) > 0
            )
            candidates = [unit for unit in eligible if unit[3] == preferred]
            if not candidates:
                candidates = eligible
            if not candidates:
                continue
            # Separate class cursors prevent a three-turn analysis burst from
            # resetting the ordinary model rotation on the next poll.
            class_key = f"{cursor_key}:{candidates[0][3]}"
            unit_turn = cursors.get(class_key, 0)
            picked = candidates[unit_turn % len(candidates)]
            spawn_plan.append(DispatchUnit(*picked, org_id))
            org_to_unit_queued[org_id][picked] -= 1
            global_capacity[picked[0]] -= 1
            if picked[2] in lane_capacity:
                lane_capacity[picked[2]] -= 1
            cursors[cursor_key] = turn + 1
            cursors[class_key] = unit_turn + 1
            cursors["org_offset"] = ordered_orgs.index(org_id) + 1
            progressed = True
        if not progressed:
            break
    return spawn_plan
