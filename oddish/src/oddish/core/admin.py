"""Admin diagnostic queries for queue slots, status, and orphaned state."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import normalize_model_id, settings
from oddish.core.cost_basis import (
    first_party_spend_filter,
    settled_cost_columns,
    settled_cost_from_row,
    settled_cost_parts,
)
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from oddish.core.quotas import (
    effective_limits_by_org_user_all_orgs,
    get_effective_org_limit,
    inflight_trial_count_by_org_user_all_orgs,
    quota_window_start,
    sum_cost_usd_by_org_user_all_orgs,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    task_experiments,
    utcnow,
)


def _normalize_owner_user_id(owner_user_id: str | None) -> str | None:
    """Hide the internal unattributed-owner sentinel from the cost UI."""
    if owner_user_id == EXPERIMENTS_UNATTRIBUTED_OWNER:
        return None
    return owner_user_id


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class QueueSlot(BaseModel):
    queue_key: str
    slot: int
    locked_by: str | None
    locked_until: datetime | None
    is_active: bool


class QueueSlotSummary(BaseModel):
    queue_key: str
    total_slots: int
    active_slots: int
    slots: list[QueueSlot]


class QueueSlotsResponse(BaseModel):
    queue_keys: list[QueueSlotSummary]
    total_slots: int
    total_active: int
    timestamp: str


class QueueStatusEntry(BaseModel):
    kind: str = "TRIAL"
    queue_key: str
    queued: int
    running: int


class QueueStatusResponse(BaseModel):
    queues: list[QueueStatusEntry] = Field(default_factory=list)
    trial_queues: list[QueueStatusEntry]
    analysis_queued: int
    analysis_running: int
    verdict_queued: int
    verdict_running: int
    timestamp: str


class OrphanedTrialSample(BaseModel):
    trial_id: str
    task_id: str
    queue_key: str
    status: str
    issue: str
    harbor_stage: str | None
    current_worker_id: str | None
    current_queue_slot: int | None
    claimed_at: datetime | None
    heartbeat_at: datetime | None
    updated_at: datetime | None


class OrphanedTaskSample(BaseModel):
    task_id: str
    status: str
    run_analysis: bool
    verdict_status: str | None
    issue: str
    updated_at: datetime | None


class OrphanedStateCounts(BaseModel):
    running_stale_heartbeat: int
    active_tasks_without_active_trials: int


class OrphanedStateResponse(BaseModel):
    counts: OrphanedStateCounts
    trial_samples: list[OrphanedTrialSample]
    task_samples: list[OrphanedTaskSample]
    stale_after_minutes: int
    timestamp: str


# ---------------------------------------------------------------------------
# worker_jobs admin
#
# Surfaces the unified queue table as a first-class admin view so
# analysis/verdict look like their own "agent jobs" rather than sidecar
# metadata on trials/tasks. Everything below reads from worker_jobs
# only; it joins to domain tables only to display context (never to
# reconstruct scheduling state).
# ---------------------------------------------------------------------------


class WorkerJobSample(BaseModel):
    id: str
    kind: str
    status: str
    queue_key: str
    subject_table: str | None
    subject_id: str | None
    attempts: int
    max_attempts: int
    claimed_at: datetime | None
    heartbeat_at: datetime | None
    stale_reaped_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    heartbeat_failure_count: int
    last_heartbeat_error: str | None
    current_worker_id: str | None
    org_id: str | None


class WorkerJobDurationStat(BaseModel):
    kind: str
    queue_key: str
    sample_count: int
    p50_seconds: float
    p95_seconds: float


class WorkerJobsResponse(BaseModel):
    """Per-kind × status counts + recent stale/failed samples.

    Counts are a dict-of-dicts so the frontend can iterate without
    knowing the enum values in advance -- new kinds automatically show
    up once they start producing rows.
    """

    counts: dict[str, dict[str, int]]
    stale_running: list[WorkerJobSample]
    recent_failures: list[WorkerJobSample]
    durations_last_hour: list[WorkerJobDurationStat]
    stale_after_minutes: int
    timestamp: str


# ---------------------------------------------------------------------------
# Core query functions
# ---------------------------------------------------------------------------


async def get_queue_slots_core(session: AsyncSession) -> QueueSlotsResponse:
    """Get current state of queue-key slot leases."""
    now = utcnow()
    result = await session.execute(
        text(
            """
            SELECT queue_key, slot, locked_by, locked_until
            FROM queue_slots
            ORDER BY queue_key, slot
            """
        )
    )
    rows = result.all()

    queue_map: dict[str, list[QueueSlot]] = {}
    for row in rows:
        queue_key = settings.normalize_queue_key(row[0])
        slot = QueueSlot(
            queue_key=queue_key,
            slot=row[1],
            locked_by=row[2],
            locked_until=row[3],
            is_active=row[2] is not None and row[3] is not None and row[3] > now,
        )
        queue_map.setdefault(queue_key, []).append(slot)

    queue_keys = []
    total_slots = 0
    total_active = 0
    for queue_key, slots in sorted(queue_map.items()):
        active_count = sum(1 for s in slots if s.is_active)
        queue_keys.append(
            QueueSlotSummary(
                queue_key=queue_key,
                total_slots=len(slots),
                active_slots=active_count,
                slots=slots,
            )
        )
        total_slots += len(slots)
        total_active += active_count

    return QueueSlotsResponse(
        queue_keys=queue_keys,
        total_slots=total_slots,
        total_active=total_active,
        timestamp=now.isoformat(),
    )


async def get_queue_status_core(session: AsyncSession) -> QueueStatusResponse:
    """Get queue status grouped by worker-job kind and queue key."""
    now = utcnow()

    # One grouped query against ``worker_jobs`` replaces three separate
    # scans on trials/analysis_status/verdict_status. The legacy aggregate
    # fields are preserved for older clients; new clients should read
    # ``queues`` so ANALYSIS and VERDICT get the same queue-key treatment
    # as TRIAL rows.
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    kind::text AS kind,
                    queue_key,
                    COUNT(*) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')) AS queued,
                    COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running
                FROM worker_jobs
                WHERE status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
                GROUP BY kind, queue_key
                ORDER BY kind, queue_key
                """
            )
        )
    ).all()

    queues: list[QueueStatusEntry] = []
    trial_queues: list[QueueStatusEntry] = []
    analysis_queued = analysis_running = 0
    verdict_queued = verdict_running = 0
    for row in rows:
        kind = row.kind
        queued = int(row.queued or 0)
        running = int(row.running or 0)
        entry = QueueStatusEntry(
            kind=kind,
            queue_key=settings.normalize_queue_key(row.queue_key),
            queued=queued,
            running=running,
        )
        queues.append(entry)
        if kind == "TRIAL":
            trial_queues.append(entry)
        elif kind == "QA":
            # The single task-level QA job (classification + verdict).
            verdict_queued += queued
            verdict_running += running
        elif kind == "ANALYSIS":
            # Legacy per-trial classification rows, drained across a deploy.
            analysis_queued += queued
            analysis_running += running
        # Unknown kinds (e.g. future QA_REVIEW) silently ignored by
        # this endpoint; the ``WorkerJobsCard`` admin panel surfaces
        # them in the kind-agnostic matrix instead.

    return QueueStatusResponse(
        queues=queues,
        trial_queues=trial_queues,
        analysis_queued=analysis_queued,
        analysis_running=analysis_running,
        verdict_queued=verdict_queued,
        verdict_running=verdict_running,
        timestamp=now.isoformat(),
    )


async def get_orphaned_state_core(
    session: AsyncSession,
    *,
    stale_after_minutes: int = 15,
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state.

    Stale-heartbeat detection reads ``worker_jobs.heartbeat_at`` --
    the authoritative scheduling-state table. ``trials.heartbeat_at``
    is a display denorm maintained in parallel; reading it here would
    duplicate the ``WorkerJobsCard`` admin panel and lie about the
    reap criterion (cleanup reaps based on ``worker_jobs``).
    Task-stuckness detection still reads domain state because the
    scheduling model of "task waiting for downstream stage to start"
    lives on the ``tasks.status`` field.
    """
    now = utcnow()

    counts_row = (
        await session.execute(
            text(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM worker_jobs wj
                        WHERE wj.kind::text = 'TRIAL'
                          AND wj.status::text = 'RUNNING'
                          AND (
                              wj.heartbeat_at IS NULL
                              OR wj.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                          )
                    ) AS running_stale_heartbeat,
                    (
                        SELECT COUNT(*)
                        FROM tasks t
                        WHERE t.deleted_at IS NULL
                          AND (
                            (
                                t.status = 'RUNNING'
                                AND NOT EXISTS (
                                    SELECT 1 FROM trials tr
                                    WHERE tr.task_id = t.id
                                      AND tr.deleted_at IS NULL
                                      AND tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                                )
                            ) OR (
                                t.status = 'ANALYZING'
                                AND NOT EXISTS (
                                    SELECT 1 FROM trials tr
                                    WHERE tr.task_id = t.id
                                      AND tr.deleted_at IS NULL
                                      AND tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                                )
                            ) OR (
                                t.status = 'VERDICT_PENDING'
                                AND (t.verdict_status IS NULL
                                     OR t.verdict_status::text NOT IN ('QUEUED', 'RUNNING'))
                            )
                          )
                    ) AS active_tasks_without_active_trials
                """
            ),
            {"stale_after_minutes": stale_after_minutes},
        )
    ).one()

    # Pull the worker_jobs samples and join back to trials for
    # display-only fields (``harbor_stage``). Scheduling-state fields
    # come from ``worker_jobs`` directly.
    trial_rows = (
        await session.execute(
            text(
                """
                SELECT
                    tr.id AS trial_id,
                    tr.task_id,
                    wj.queue_key,
                    tr.status::text AS status,
                    'running_stale_heartbeat'::text AS issue,
                    tr.harbor_stage,
                    wj.current_worker_id,
                    wj.current_queue_slot,
                    wj.claimed_at,
                    wj.heartbeat_at,
                    tr.updated_at
                FROM worker_jobs wj
                JOIN trials tr ON wj.subject_table = 'trials' AND wj.subject_id = tr.id
                WHERE wj.kind::text = 'TRIAL'
                  AND wj.status::text = 'RUNNING'
                  AND tr.deleted_at IS NULL
                  AND (
                      wj.heartbeat_at IS NULL
                      OR wj.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                  )
                ORDER BY wj.heartbeat_at ASC NULLS FIRST
                LIMIT 20
                """
            ),
            {"stale_after_minutes": stale_after_minutes},
        )
    ).all()

    task_rows = (
        await session.execute(
            text(
                """
                SELECT
                    t.id AS task_id,
                    t.status::text AS status,
                    t.run_analysis,
                    t.verdict_status::text AS verdict_status,
                    'active_task_without_active_trials'::text AS issue,
                    t.updated_at
                FROM tasks t
                WHERE t.deleted_at IS NULL
                  AND (
                    (
                        t.status = 'RUNNING'
                        AND NOT EXISTS (
                            SELECT 1 FROM trials tr
                            WHERE tr.task_id = t.id
                              AND tr.deleted_at IS NULL
                              AND tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                        )
                    ) OR (
                        t.status = 'ANALYZING'
                        AND NOT EXISTS (
                            SELECT 1 FROM trials tr
                            WHERE tr.task_id = t.id
                              AND tr.deleted_at IS NULL
                              AND tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                        )
                    ) OR (
                        t.status = 'VERDICT_PENDING'
                        AND (t.verdict_status IS NULL
                             OR t.verdict_status::text NOT IN ('QUEUED', 'RUNNING'))
                    )
                  )
                ORDER BY t.updated_at ASC NULLS FIRST
                LIMIT 20
                """
            )
        )
    ).all()

    return OrphanedStateResponse(
        counts=OrphanedStateCounts(
            running_stale_heartbeat=int(counts_row.running_stale_heartbeat or 0),
            active_tasks_without_active_trials=int(
                counts_row.active_tasks_without_active_trials or 0
            ),
        ),
        trial_samples=[
            OrphanedTrialSample(
                trial_id=row.trial_id,
                task_id=row.task_id,
                queue_key=settings.normalize_queue_key(row.queue_key),
                status=row.status,
                issue=row.issue,
                harbor_stage=row.harbor_stage,
                current_worker_id=row.current_worker_id,
                current_queue_slot=row.current_queue_slot,
                claimed_at=row.claimed_at,
                heartbeat_at=row.heartbeat_at,
                updated_at=row.updated_at,
            )
            for row in trial_rows
        ],
        task_samples=[
            OrphanedTaskSample(
                task_id=row.task_id,
                status=row.status,
                run_analysis=bool(row.run_analysis),
                verdict_status=row.verdict_status,
                issue=row.issue,
                updated_at=row.updated_at,
            )
            for row in task_rows
        ],
        stale_after_minutes=stale_after_minutes,
        timestamp=now.isoformat(),
    )


async def get_worker_jobs_admin_core(
    session: AsyncSession,
    *,
    stale_after_minutes: int = 15,
    sample_limit: int = 25,
) -> WorkerJobsResponse:
    """Summarize the unified ``worker_jobs`` table for the admin page.

    Returns a matrix of ``{kind: {status: count}}`` plus recent
    diagnostic samples: RUNNING rows with a stale heartbeat, the most
    recently FAILED rows, and per-kind × queue_key duration
    percentiles over the last hour. Everything is derived from
    ``worker_jobs`` alone -- domain tables are not involved.
    """
    now = utcnow()

    # -- counts matrix -----------------------------------------------------
    count_rows = (
        await session.execute(
            text(
                """
                SELECT kind::text AS kind,
                       status::text AS status,
                       COUNT(*) AS n
                FROM   worker_jobs
                GROUP  BY kind, status
                """
            )
        )
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for row in count_rows:
        counts.setdefault(row.kind, {})[row.status] = int(row.n or 0)

    # -- stale RUNNING -----------------------------------------------------
    stale_running_rows = (
        await session.execute(
            text(
                """
                SELECT id,
                       kind::text AS kind,
                       status::text AS status,
                       queue_key,
                       subject_table,
                       subject_id,
                       attempts,
                       max_attempts,
                       claimed_at,
                       heartbeat_at,
                       stale_reaped_at,
                       finished_at,
                       error_message,
                       heartbeat_failure_count,
                       last_heartbeat_error,
                       current_worker_id,
                       org_id
                FROM   worker_jobs
                WHERE  status::text = 'RUNNING'
                  AND  (
                      heartbeat_at IS NULL
                      OR heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                  )
                ORDER  BY heartbeat_at ASC NULLS FIRST
                LIMIT  :sample_limit
                """
            ),
            {
                "stale_after_minutes": stale_after_minutes,
                "sample_limit": sample_limit,
            },
        )
    ).all()

    # -- recent failures ---------------------------------------------------
    recent_failure_rows = (
        await session.execute(
            text(
                """
                SELECT id,
                       kind::text AS kind,
                       status::text AS status,
                       queue_key,
                       subject_table,
                       subject_id,
                       attempts,
                       max_attempts,
                       claimed_at,
                       heartbeat_at,
                       stale_reaped_at,
                       finished_at,
                       error_message,
                       heartbeat_failure_count,
                       last_heartbeat_error,
                       current_worker_id,
                       org_id
                FROM   worker_jobs
                WHERE  status::text IN ('FAILED', 'CANCELLED')
                ORDER  BY finished_at DESC NULLS LAST
                LIMIT  :sample_limit
                """
            ),
            {"sample_limit": sample_limit},
        )
    ).all()

    def _sample(row) -> WorkerJobSample:
        return WorkerJobSample(
            id=row.id,
            kind=row.kind,
            status=row.status,
            queue_key=settings.normalize_queue_key(row.queue_key),
            subject_table=row.subject_table,
            subject_id=row.subject_id,
            attempts=int(row.attempts or 0),
            max_attempts=int(row.max_attempts or 0),
            claimed_at=row.claimed_at,
            heartbeat_at=row.heartbeat_at,
            stale_reaped_at=row.stale_reaped_at,
            finished_at=row.finished_at,
            error_message=row.error_message,
            heartbeat_failure_count=int(row.heartbeat_failure_count or 0),
            last_heartbeat_error=row.last_heartbeat_error,
            current_worker_id=row.current_worker_id,
            org_id=row.org_id,
        )

    stale_running = [_sample(r) for r in stale_running_rows]
    recent_failures = [_sample(r) for r in recent_failure_rows]

    # -- per-kind × queue_key duration percentiles ------------------------
    # Only jobs that actually completed (claimed_at + finished_at) count
    # toward the duration distribution. Percent_cont is exact on
    # Postgres and doesn't need a window function -- we're already
    # grouping.
    duration_rows = (
        await session.execute(
            text(
                """
                SELECT kind::text AS kind,
                       queue_key,
                       COUNT(*) AS n,
                       percentile_cont(0.50) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (finished_at - claimed_at))
                       ) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (finished_at - claimed_at))
                       ) AS p95
                FROM   worker_jobs
                WHERE  status::text IN ('SUCCESS', 'FAILED')
                  AND  claimed_at IS NOT NULL
                  AND  finished_at IS NOT NULL
                  AND  finished_at >= NOW() - INTERVAL '1 hour'
                GROUP  BY kind, queue_key
                HAVING COUNT(*) >= 3
                ORDER  BY kind, queue_key
                """
            )
        )
    ).all()

    durations_last_hour = [
        WorkerJobDurationStat(
            kind=row.kind,
            queue_key=settings.normalize_queue_key(row.queue_key),
            sample_count=int(row.n or 0),
            p50_seconds=float(row.p50 or 0.0),
            p95_seconds=float(row.p95 or 0.0),
        )
        for row in duration_rows
    ]

    return WorkerJobsResponse(
        counts=counts,
        stale_running=stale_running,
        recent_failures=recent_failures,
        durations_last_hour=durations_last_hour,
        stale_after_minutes=stale_after_minutes,
        timestamp=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# Queue health overview
#
# A single operator-facing answer to "is the queue keeping up?": throughput
# (jobs started/finished per window), per-queue-key capacity fill (running vs
# configured concurrency limit) with the oldest queued age and time-in-queue
# percentiles, plus the persisted dispatcher/reconciler heartbeats. This is
# the panel that lets an operator self-diagnose "queued but not running"
# without dropping into psql + Modal logs.
# ---------------------------------------------------------------------------


class QueueThroughputStat(BaseModel):
    kind: str
    started_5m: int
    started_15m: int
    started_60m: int
    finished_5m: int
    finished_15m: int
    finished_60m: int


class QueueCapacityStat(BaseModel):
    queue_key: str
    queued: int
    queued_scheduled: int
    running: int
    limit: int
    # Fraction running / limit in [0, 1+] (can exceed 1 if a limit was lowered
    # below the current running count). None when limit is 0.
    fill: float | None
    oldest_queued_age_seconds: float | None
    wait_p50_seconds: float | None
    wait_p95_seconds: float | None


class QueueRuntimeComponentStatus(BaseModel):
    component: str
    updated_at: datetime | None
    age_seconds: float | None
    payload: dict[str, Any] = Field(default_factory=dict)


class QueueHealthResponse(BaseModel):
    totals_queued: int
    totals_running: int
    throughput: list[QueueThroughputStat]
    capacity: list[QueueCapacityStat]
    dispatcher: QueueRuntimeComponentStatus | None
    reconciler: QueueRuntimeComponentStatus | None
    timestamp: str


async def get_queue_health_core(session: AsyncSession) -> QueueHealthResponse:
    """Aggregate throughput, per-queue-key capacity fill, and component health."""
    now = utcnow()

    # -- throughput per kind ----------------------------------------------
    throughput_rows = (
        await session.execute(
            text(
                """
                SELECT kind::text AS kind,
                       COUNT(*) FILTER (WHERE started_at  >= NOW() - INTERVAL '5 minutes')  AS started_5m,
                       COUNT(*) FILTER (WHERE started_at  >= NOW() - INTERVAL '15 minutes') AS started_15m,
                       COUNT(*) FILTER (WHERE started_at  >= NOW() - INTERVAL '60 minutes') AS started_60m,
                       COUNT(*) FILTER (WHERE finished_at >= NOW() - INTERVAL '5 minutes')  AS finished_5m,
                       COUNT(*) FILTER (WHERE finished_at >= NOW() - INTERVAL '15 minutes') AS finished_15m,
                       COUNT(*) FILTER (WHERE finished_at >= NOW() - INTERVAL '60 minutes') AS finished_60m
                FROM   worker_jobs
                WHERE  started_at  >= NOW() - INTERVAL '60 minutes'
                   OR  finished_at >= NOW() - INTERVAL '60 minutes'
                GROUP  BY kind
                ORDER  BY kind
                """
            )
        )
    ).all()
    throughput = [
        QueueThroughputStat(
            kind=row.kind,
            started_5m=int(row.started_5m or 0),
            started_15m=int(row.started_15m or 0),
            started_60m=int(row.started_60m or 0),
            finished_5m=int(row.finished_5m or 0),
            finished_15m=int(row.finished_15m or 0),
            finished_60m=int(row.finished_60m or 0),
        )
        for row in throughput_rows
    ]

    # -- per-queue-key queued / running / oldest-age ----------------------
    capacity_rows = (
        await session.execute(
            text(
                """
                SELECT queue_key,
                       COUNT(*) FILTER (
                           WHERE status::text IN ('QUEUED', 'RETRYING')
                             AND available_after <= NOW()
                       ) AS queued_ready,
                       COUNT(*) FILTER (
                           WHERE status::text IN ('QUEUED', 'RETRYING')
                             AND available_after > NOW()
                       ) AS queued_scheduled,
                       COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running,
                       EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (
                           WHERE status::text IN ('QUEUED', 'RETRYING')
                             AND available_after <= NOW()
                       ))) AS oldest_queued_age_seconds
                FROM   worker_jobs
                WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
                GROUP  BY queue_key
                """
            )
        )
    ).all()

    # -- time-in-queue percentiles per queue_key (claimed in last hour) ---
    wait_rows = (
        await session.execute(
            text(
                """
                SELECT queue_key,
                       percentile_cont(0.50) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (claimed_at - created_at))
                       ) AS wait_p50,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (claimed_at - created_at))
                       ) AS wait_p95
                FROM   worker_jobs
                WHERE  claimed_at IS NOT NULL
                  AND  claimed_at >= NOW() - INTERVAL '1 hour'
                  AND  claimed_at >= created_at
                GROUP  BY queue_key
                HAVING COUNT(*) >= 3
                """
            )
        )
    ).all()
    wait_by_key: dict[str, tuple[float | None, float | None]] = {}
    for row in wait_rows:
        key = settings.normalize_queue_key(row.queue_key)
        wait_by_key[key] = (
            float(row.wait_p50) if row.wait_p50 is not None else None,
            float(row.wait_p95) if row.wait_p95 is not None else None,
        )

    # Merge per queue_key (normalizing collapses aliases onto one bucket).
    merged: dict[str, dict[str, float | None]] = {}
    for row in capacity_rows:
        key = settings.normalize_queue_key(row.queue_key)
        bucket = merged.setdefault(
            key,
            {"queued": 0, "queued_scheduled": 0, "running": 0, "oldest": None},
        )
        bucket["queued"] = (bucket["queued"] or 0) + int(row.queued_ready or 0)
        bucket["queued_scheduled"] = (bucket["queued_scheduled"] or 0) + int(
            row.queued_scheduled or 0
        )
        bucket["running"] = (bucket["running"] or 0) + int(row.running or 0)
        age = (
            float(row.oldest_queued_age_seconds)
            if row.oldest_queued_age_seconds is not None
            else None
        )
        if age is not None:
            current = bucket["oldest"]
            bucket["oldest"] = age if current is None else max(current, age)

    capacity: list[QueueCapacityStat] = []
    for key, bucket in merged.items():
        limit = settings.get_model_concurrency(key)
        running = int(bucket["running"] or 0)
        wait_p50, wait_p95 = wait_by_key.get(key, (None, None))
        capacity.append(
            QueueCapacityStat(
                queue_key=key,
                queued=int(bucket["queued"] or 0),
                queued_scheduled=int(bucket["queued_scheduled"] or 0),
                running=running,
                limit=limit,
                fill=(running / limit) if limit > 0 else None,
                oldest_queued_age_seconds=bucket["oldest"],
                wait_p50_seconds=wait_p50,
                wait_p95_seconds=wait_p95,
            )
        )

    # Most-pressured first: deepest backlog, then highest fill.
    capacity.sort(key=lambda c: (c.queued, c.running), reverse=True)

    totals_queued = sum(c.queued for c in capacity)
    totals_running = sum(c.running for c in capacity)

    # -- persisted dispatcher / reconciler heartbeats ---------------------
    # Lazy import keeps the worker-only import chain out of the server-only
    # install; the queue-health endpoint is hosted-backend only.
    from oddish.workers.queue.runtime_status import (
        DISPATCHER_COMPONENT,
        RECONCILER_COMPONENT,
        get_queue_runtime_statuses,
    )

    statuses = await get_queue_runtime_statuses(session)

    def _component(name: str) -> QueueRuntimeComponentStatus | None:
        row = statuses.get(name)
        if row is None:
            return None
        updated_at = row.get("updated_at")
        age_seconds: float | None = None
        if isinstance(updated_at, datetime):
            age_seconds = max((now - updated_at).total_seconds(), 0.0)
        return QueueRuntimeComponentStatus(
            component=name,
            updated_at=updated_at,
            age_seconds=age_seconds,
            payload=row.get("payload") or {},
        )

    return QueueHealthResponse(
        totals_queued=totals_queued,
        totals_running=totals_running,
        throughput=throughput,
        capacity=capacity,
        dispatcher=_component(DISPATCHER_COMPONENT),
        reconciler=_component(RECONCILER_COMPONENT),
        timestamp=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# Live submission-load snapshot (advertised header + GET /load)
#
# A slim, ~5s-cached, single-flighted view derived from get_queue_health_core.
# Inert until the client/runtime consume it.
# ---------------------------------------------------------------------------

LOAD_CACHE_TTL_SECONDS = 5.0
CLIENT_FLOOR = 4
CLIENT_CEILING_MAX = 64
PRESSURE_WAIT_TARGET_S = 120.0
PRESSURE_SOFT_QUEUE_CAP = 500.0
PRESSURE_RTT_BUDGET_S = 2.0
SUBMIT_CONCURRENCY_HEADER = "Oddish-Submit-Concurrency"
SUBMIT_LATENCY_COMPONENT = "submit_latency"

# Indirection so tests can advance the clock deterministically.
_monotonic = time.monotonic


def compute_pressure(
    *,
    wait_p95_max: float | None,
    totals_queued: int,
    sweep_rtt_p95_ewma: float | None,
) -> float:
    """Saturation score in [0, 1] = max of the normalized signal terms."""
    wait_term = (wait_p95_max or 0.0) / PRESSURE_WAIT_TARGET_S
    queue_term = float(totals_queued) / PRESSURE_SOFT_QUEUE_CAP
    rtt_term = (sweep_rtt_p95_ewma or 0.0) / PRESSURE_RTT_BUDGET_S
    return max(0.0, min(1.0, max(wait_term, queue_term, rtt_term)))


def compute_submit_ceiling(pressure: float) -> int:
    """Recommended client in-flight submission ceiling: full when idle, floor when saturated."""
    raw = round(CLIENT_CEILING_MAX * (1.0 - pressure))
    return int(max(CLIENT_FLOOR, min(CLIENT_CEILING_MAX, raw)))


class LoadTotals(BaseModel):
    queued: int
    running: int


class LoadQueue(BaseModel):
    queue_key: str
    queued: int
    running: int
    limit: int
    fill: float | None
    wait_p95_seconds: float | None
    advisory_limit: int  # == limit until a dynamic advisory limit lands
    limit_source: str  # "static" until a dynamic advisory limit lands


class LoadSnapshot(BaseModel):
    submit_ceiling: int
    pressure: float
    ttl_seconds: int
    totals: LoadTotals
    queues: list[LoadQueue]
    timestamp: str


async def build_load_snapshot(session: AsyncSession) -> LoadSnapshot:
    """Derive the slim load snapshot from the existing queue-health aggregate."""
    health = await get_queue_health_core(session)

    # Lazy import keeps the worker-only chain out of the server-only install
    # (mirrors get_queue_health_core).
    from oddish.workers.queue.runtime_status import get_queue_runtime_statuses

    statuses = await get_queue_runtime_statuses(session)
    # `sweep_rtt_p95_ewma` is a contract-mandated key name: the value is actually a
    # smoothed MEAN of submission-handler latency (an EWMA, not a true p95) and pools
    # /tasks/sweep with /tasks/upload/*. Kept verbatim so the read/write keys match.
    submit_row = statuses.get(SUBMIT_LATENCY_COMPONENT) or {}
    sweep_rtt = (submit_row.get("payload") or {}).get("sweep_rtt_p95_ewma")

    wait_values = [
        c.wait_p95_seconds for c in health.capacity if c.wait_p95_seconds is not None
    ]
    wait_p95_max = max(wait_values) if wait_values else None

    pressure = compute_pressure(
        wait_p95_max=wait_p95_max,
        totals_queued=health.totals_queued,
        sweep_rtt_p95_ewma=float(sweep_rtt) if sweep_rtt is not None else None,
    )
    queues = [
        LoadQueue(
            queue_key=c.queue_key,
            queued=c.queued,
            running=c.running,
            limit=c.limit,
            fill=c.fill,
            wait_p95_seconds=c.wait_p95_seconds,
            advisory_limit=c.limit,
            limit_source="static",
        )
        for c in health.capacity
    ]
    return LoadSnapshot(
        submit_ceiling=compute_submit_ceiling(pressure),
        pressure=pressure,
        ttl_seconds=int(LOAD_CACHE_TTL_SECONDS),
        totals=LoadTotals(queued=health.totals_queued, running=health.totals_running),
        queues=queues,
        timestamp=health.timestamp,
    )


_load_cache: LoadSnapshot | None = None
_load_cache_at: float = 0.0
_load_cache_lock = asyncio.Lock()


async def _refresh_load_snapshot() -> LoadSnapshot:
    """Open a session and rebuild the snapshot (the only DB-touching seam)."""
    from oddish.db import get_session

    async with get_session() as session:
        return await build_load_snapshot(session)


async def get_cached_load_snapshot(ttl: float = LOAD_CACHE_TTL_SECONDS) -> LoadSnapshot:
    """Return the slim load snapshot, refreshing at most once per ``ttl`` seconds.

    Single-flighted: under a burst, exactly one coroutine refreshes while the
    rest wait on the lock and then read the just-refreshed value, so the
    underlying queue-health query runs at most once per ttl per container.
    """
    global _load_cache, _load_cache_at
    if _load_cache is not None and _monotonic() - _load_cache_at < ttl:
        return _load_cache
    async with _load_cache_lock:
        if _load_cache is not None and _monotonic() - _load_cache_at < ttl:
            return _load_cache
        _load_cache = await _refresh_load_snapshot()
        _load_cache_at = _monotonic()
        return _load_cache


# ---------------------------------------------------------------------------
# Cost breakdown: global admin spend view over billable trials only.
# ---------------------------------------------------------------------------

_MAX_MODELS_PER_USER = 6
_MAX_MODELS_PER_EXPERIMENT = 12

_UNATTRIBUTED_KEY = "__unattributed__"


def _real_spend_filter():
    """WHERE clause selecting real, first-party oddish spend, counted once.

    The shared first-party filter excludes imports and experiment-combine
    copies. Billed probes remain visible because they consumed quota; unbilled
    probes stay internal. Other unbilled Oddish runs remain visible so
    offboarded, unlinked, and pre-quota spend is not silently dropped.
    """
    return and_(
        first_party_spend_filter(),
        or_(
            TrialModel.billed_user_id.isnot(None),
            TrialModel.is_probe.is_(False),
        ),
    )


def _spend_identity(
    billed_user_id: str | None,
    github_id: str | None,
    github_username: str | None,
    submitter_user_id: str | None,
) -> tuple[str, str | None, str | None]:
    """Attribute a trial's spend to a payer, falling back past unregistered spend.

    Precedence: the active billed user, else the submitted GitHub identity (id,
    then handle), else the submitting credential's user, else one Unattributed
    bucket. Returns ``(key, real_user_id, label)``: ``key`` dedups both the
    by-user rows and the by-user series; ``real_user_id`` is the actual user
    whose name labels the row (the billed user or the submitter), or None for a
    GitHub-handle / Unattributed row; ``label`` is a precomputed display label,
    or None when ``real_user_id`` supplies the name via the enrichment step.

    Whether a row is drilldown-linkable is decided by the caller, not here: any
    row with a ``real_user_id`` links to that user's drilldown, even when it
    also holds unbilled spend. The per-user drilldown sums billed spend alone,
    so an unbilled row's drilldown total may fall short of the row total -- the
    caller flags that with ``has_unbilled_spend`` rather than dropping the link.
    """
    if billed_user_id:
        return billed_user_id, billed_user_id, None
    github_id = (github_id or "").strip() or None
    github_username = (github_username or "").strip() or None
    if github_id:
        return (
            f"ghid:{github_id}",
            None,
            f"@{github_username}" if github_username else f"github:{github_id}",
        )
    if github_username:
        return f"ghuser:{github_username.lower()}", None, f"@{github_username}"
    if submitter_user_id:
        return submitter_user_id, submitter_user_id, None
    return _UNATTRIBUTED_KEY, None, "Unattributed"


def _series_bucket(window_days: int | None) -> str:
    """Pick a chart bucket size that fits the window."""
    if window_days is not None and window_days <= 2:
        return "hour"
    if window_days is not None and window_days <= 120:
        return "day"
    return "week"


class CostModelBreakdown(BaseModel):
    model: str
    provider: str
    trial_count: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_estimated_usd: float


class CostUserBreakdown(BaseModel):
    # Stable grouping key: a user id for billed/submitter rows, else a synthetic
    # ``ghid:``/``ghuser:``/``__unattributed__`` key for label-only fallback rows.
    key: str
    # Deep-link target: set whenever the row resolves to a real oddish user (the
    # billed user or the submitting credential's user), even if some/all of its
    # trials are unbilled. None => the row is a GitHub-handle / Unattributed
    # fallback that is not a registered user, so it renders non-clickable.
    owner_user_id: str | None
    # True when the row includes trials that were never billed to a quota (e.g.
    # spend created before billing stamping shipped, or an offboarded payer).
    # Drives the "unbilled" chip; for a linkable row it also warns that the
    # per-user drilldown (billed spend only) may total less than this row.
    has_unbilled_spend: bool
    # Precomputed label for a row with no backing user (GitHub handle,
    # "Unattributed"); None means fill the display name from the linked user.
    label: str | None = None
    org_id: str | None
    name: str | None = None
    email: str | None = None
    org_name: str | None = None
    trial_count: int
    experiment_count: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_estimated_usd: float
    models: list[CostModelBreakdown]
    prev_cost_usd: float | None = None
    inflight_trial_count: int = 0
    quota_spent_usd: float | None = None
    quota_limit_usd: float | None = None


class CostExperimentBreakdown(BaseModel):
    experiment_id: str
    name: str | None
    org_id: str | None
    owner_user_id: str | None
    owner_name: str | None = None
    owner_email: str | None = None
    owner_label: str | None = None
    org_name: str | None = None
    created_at: datetime | None
    last_activity_at: datetime | None
    trial_count: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_estimated_usd: float
    models: list[CostModelBreakdown]


class CostSeriesKey(BaseModel):
    """One legend entry in a cost-over-time chart."""

    key: str
    label: str


class CostSeriesBucket(BaseModel):
    """One time bucket: total spend plus its per-key split."""

    bucket_start: datetime
    cost_usd: float
    trial_count: int
    costs: dict[str, float]


class CostSeries(BaseModel):
    """Cost over time, stacked by one dimension."""

    dimension: str
    keys: list[CostSeriesKey]
    buckets: list[CostSeriesBucket]


class CostTotals(BaseModel):
    window_days: int | None
    trial_count: int
    experiment_count: int
    user_count: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_native_usd: float
    cost_estimated_usd: float
    prev_cost_usd: float | None = None
    month_cost_usd: float = 0.0
    month_budget_usd: float | None = None


class CostBreakdownResponse(BaseModel):
    window_days: int | None
    bucket: str
    series_by_agent: CostSeries
    series_by_model: CostSeries
    series_by_user: CostSeries
    totals: CostTotals
    by_user: list[CostUserBreakdown]
    by_model: list[CostModelBreakdown]
    experiments: list[CostExperimentBreakdown]
    timestamp: str


def _model_label(model: str | None) -> str:
    """Canonicalize a model id so spellings collapse onto one row."""
    return normalize_model_id(model) or "unknown"


def _provider_label(provider: str | None) -> str:
    return (provider or "").strip().lower() or "unknown"


def _accumulate_model(
    bucket: dict[tuple[str, str], dict[str, Any]],
    *,
    model: str,
    provider: str,
    trial_count: int,
    input_tokens: int,
    cache_tokens: int,
    output_tokens: int,
    cost_usd: float,
    cost_estimated_usd: float,
) -> None:
    key = (model, provider)
    agg = bucket.get(key)
    if agg is None:
        agg = bucket[key] = {
            "model": model,
            "provider": provider,
            "trial_count": 0,
            "input_tokens": 0,
            "cache_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "cost_estimated_usd": 0.0,
        }
    agg["trial_count"] += trial_count
    agg["input_tokens"] += input_tokens
    agg["cache_tokens"] += cache_tokens
    agg["output_tokens"] += output_tokens
    agg["cost_usd"] += cost_usd
    agg["cost_estimated_usd"] += cost_estimated_usd


def _model_breakdowns(
    bucket: dict[tuple[str, str], dict[str, Any]], *, limit: int | None = None
) -> list[CostModelBreakdown]:
    rows = sorted(bucket.values(), key=lambda m: m["cost_usd"], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return [
        CostModelBreakdown(
            model=str(m["model"]),
            provider=str(m["provider"]),
            trial_count=int(m["trial_count"]),
            input_tokens=int(m["input_tokens"]),
            cache_tokens=int(m["cache_tokens"]),
            output_tokens=int(m["output_tokens"]),
            cost_usd=round(float(m["cost_usd"]), 4),
            cost_estimated_usd=round(float(m["cost_estimated_usd"]), 4),
        )
        for m in rows
    ]


_SERIES_TOP_N = 8
_SERIES_OTHER_KEY = "__other__"


def _build_dimension_series(
    dimension: str,
    *,
    bucket_starts: list[datetime],
    per_bucket: dict[datetime, dict[str, float]],
    totals: dict[str, float],
    trials_per_bucket: dict[datetime, int],
    labels: dict[str, str],
) -> CostSeries:
    """Keep the top-spend keys and fold the rest into one "Other" stack."""
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top_keys = [k for k, _ in ranked[:_SERIES_TOP_N]]
    top_set = set(top_keys)
    has_other = len(totals) > len(top_set)

    keys = [CostSeriesKey(key=k, label=labels.get(k, k)) for k in top_keys]
    if has_other:
        keys.append(CostSeriesKey(key=_SERIES_OTHER_KEY, label="Other"))

    buckets: list[CostSeriesBucket] = []
    for bstart in bucket_starts:
        per_key = per_bucket.get(bstart, {})
        folded: dict[str, float] = {}
        other = 0.0
        total = 0.0
        for k, value in per_key.items():
            total += value
            if k in top_set:
                folded[k] = folded.get(k, 0.0) + value
            else:
                other += value
        if has_other and other > 0:
            folded[_SERIES_OTHER_KEY] = other
        buckets.append(
            CostSeriesBucket(
                bucket_start=bstart,
                cost_usd=round(total, 4),
                trial_count=trials_per_bucket.get(bstart, 0),
                costs={k: round(v, 4) for k, v in folded.items()},
            )
        )
    return CostSeries(dimension=dimension, keys=keys, buckets=buckets)


async def _cost_time_series(
    session: AsyncSession, *, since: datetime | None, bucket: str
) -> tuple[CostSeries, CostSeries, CostSeries]:
    """Billable cost over time, stacked three ways: by agent, model, and user.

    Settlement-time axis (``finished_at``): in-flight trials (``finished_at``
    NULL) are excluded so this matches the quota basis exactly.
    """
    bucket_col = func.date_trunc(bucket, TrialModel.finished_at)
    gh_id_col = TaskModel.tags["github_id"].astext
    gh_user_col = TaskModel.tags["github_username"].astext

    query = (
        select(
            bucket_col.label("bucket"),
            TrialModel.agent.label("agent"),
            TrialModel.model.label("model"),
            TrialModel.billed_user_id.label("billed_user_id"),
            gh_id_col.label("gh_id"),
            gh_user_col.label("gh_user"),
            TaskModel.created_by_user_id.label("submitter"),
            *settled_cost_columns(),
            func.count(TrialModel.id).label("trial_count"),
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        # LEFT join so a soft-deleted task yields NULL tags (the trial's spend
        # still counts, attributed by the fallback) rather than dropping the row.
        .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
        .group_by(
            bucket_col,
            TrialModel.agent,
            TrialModel.model,
            TrialModel.billed_user_id,
            gh_id_col,
            gh_user_col,
            TaskModel.created_by_user_id,
        )
    )
    query = query.where(_real_spend_filter(), TrialModel.finished_at.isnot(None))
    if since is not None:
        query = query.where(TrialModel.finished_at >= since)

    rows = (await session.execute(query)).all()

    agent_per_bucket: dict[datetime, dict[str, float]] = {}
    agent_totals: dict[str, float] = {}
    model_per_bucket: dict[datetime, dict[str, float]] = {}
    model_totals: dict[str, float] = {}
    user_per_bucket: dict[datetime, dict[str, float]] = {}
    user_totals: dict[str, float] = {}
    # Legend labels for fallback user keys (GitHub handle / "Unattributed").
    # Billed/submitter keys are real user ids and stay unlabeled here so the
    # enrichment step resolves them to a name.
    user_labels: dict[str, str] = {}
    trials_per_bucket: dict[datetime, int] = {}

    def _add(
        per_bucket: dict[datetime, dict[str, float]],
        totals: dict[str, float],
        bstart: datetime,
        key: str,
        cost: float,
    ) -> None:
        slot = per_bucket.setdefault(bstart, {})
        slot[key] = slot.get(key, 0.0) + cost
        totals[key] = totals.get(key, 0.0) + cost

    for row in rows:
        cost = settled_cost_from_row(row)
        bstart = row.bucket
        u_key, _u_real, u_label = _spend_identity(
            row.billed_user_id, row.gh_id, row.gh_user, row.submitter
        )
        if u_label is not None:
            user_labels[u_key] = u_label
        _add(agent_per_bucket, agent_totals, bstart, row.agent or "unknown", cost)
        _add(model_per_bucket, model_totals, bstart, _model_label(row.model), cost)
        _add(user_per_bucket, user_totals, bstart, u_key, cost)
        trials_per_bucket[bstart] = trials_per_bucket.get(bstart, 0) + int(
            row.trial_count or 0
        )

    bucket_starts = sorted(trials_per_bucket.keys())

    by_agent = _build_dimension_series(
        "agent",
        bucket_starts=bucket_starts,
        per_bucket=agent_per_bucket,
        totals=agent_totals,
        trials_per_bucket=trials_per_bucket,
        labels={},
    )
    by_model = _build_dimension_series(
        "model",
        bucket_starts=bucket_starts,
        per_bucket=model_per_bucket,
        totals=model_totals,
        trials_per_bucket=trials_per_bucket,
        labels={},
    )
    by_user = _build_dimension_series(
        "user",
        bucket_starts=bucket_starts,
        per_bucket=user_per_bucket,
        totals=user_totals,
        trials_per_bucket=trials_per_bucket,
        labels=user_labels,
    )
    return by_agent, by_model, by_user


def _clean_author(value: str | None) -> str | None:
    """Ignore blank and placeholder 'unknown' author strings."""
    cleaned = (value or "").strip()
    if not cleaned or cleaned.lower() == "unknown":
        return None
    return cleaned


async def _primary_task_authors(
    session: AsyncSession, experiment_ids: list[str]
) -> dict[str, str]:
    """Get each experiment's oldest-task author, used as a fallback owner name."""
    if not experiment_ids:
        return {}
    github_tag = TaskModel.tags["github_username"].astext
    rows = (
        await session.execute(
            select(
                task_experiments.c.experiment_id.label("experiment_id"),
                github_tag.label("github_username"),
                TaskModel.user.label("user"),
            )
            .select_from(
                task_experiments.join(
                    TaskModel, TaskModel.id == task_experiments.c.task_id
                )
            )
            .where(task_experiments.c.experiment_id.in_(experiment_ids))
            .where(task_experiments.c.deleted_at.is_(None))
            .order_by(
                task_experiments.c.experiment_id.asc(),
                TaskModel.created_at.asc(),
                TaskModel.id.asc(),
            )
            .distinct(task_experiments.c.experiment_id)
        )
    ).all()
    authors: dict[str, str] = {}
    for row in rows:
        name = _clean_author(row.github_username) or _clean_author(row.user)
        if name:
            authors[str(row.experiment_id)] = name
    return authors


async def _prev_window_costs(
    session: AsyncSession, *, prev_start: datetime, prev_end: datetime
) -> tuple[dict[tuple[str | None, str], float], float]:
    """Per-(org, payer) spend and total spend for the prior adjacent window.

    Mirrors the main breakdown's basis exactly: same joins (so the session's
    soft-delete filtering applies), same real-spend gate, same payer identity.
    """
    gh_id_col = TaskModel.tags["github_id"].astext
    gh_user_col = TaskModel.tags["github_username"].astext
    query = (
        select(
            TrialModel.org_id.label("org_id"),
            TrialModel.billed_user_id.label("billed_user_id"),
            gh_id_col.label("gh_id"),
            gh_user_col.label("gh_user"),
            TaskModel.created_by_user_id.label("submitter"),
            TrialModel.model.label("model"),
            *settled_cost_columns(),
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
        .where(
            _real_spend_filter(),
            TrialModel.finished_at >= prev_start,
            TrialModel.finished_at < prev_end,
        )
        .group_by(
            TrialModel.org_id,
            TrialModel.billed_user_id,
            gh_id_col,
            gh_user_col,
            TaskModel.created_by_user_id,
            TrialModel.model,
        )
    )
    rows = (await session.execute(query)).all()

    prev_by_user: dict[tuple[str | None, str], float] = {}
    prev_cost = 0.0
    for row in rows:
        cost = settled_cost_from_row(row)
        identity_key, _, _ = _spend_identity(
            row.billed_user_id, row.gh_id, row.gh_user, row.submitter
        )
        key = (row.org_id, identity_key)
        prev_by_user[key] = prev_by_user.get(key, 0.0) + cost
        prev_cost += cost
    return prev_by_user, round(prev_cost, 4)


def _org_id_predicate(org_ids: set[str | None]):
    non_null_org_ids = [org_id for org_id in org_ids if org_id is not None]
    predicates = []
    if non_null_org_ids:
        predicates.append(TrialModel.org_id.in_(non_null_org_ids))
    if None in org_ids:
        predicates.append(TrialModel.org_id.is_(None))
    return or_(*predicates)


async def _billed_cost_since(
    session: AsyncSession,
    *,
    since: datetime,
    org_ids: set[str | None] | None = None,
) -> float:
    """Total dashboard spend from ``since`` to now, on the breakdown's basis.

    Settlement-time axis: sums trials that FINISHED at/after ``since``.
    """
    query = (
        select(
            TrialModel.model.label("model"),
            *settled_cost_columns(),
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
        .where(
            _real_spend_filter(),
            TrialModel.finished_at >= since,
        )
        .group_by(TrialModel.model)
    )
    if org_ids is not None:
        if not org_ids:
            return 0.0
        query = query.where(_org_id_predicate(org_ids))
    rows = (await session.execute(query)).all()
    total = sum(settled_cost_from_row(row) for row in rows)
    return round(total, 4)


async def get_cost_breakdown_core(
    session: AsyncSession,
    *,
    window_days: int | None = 7,
    experiment_limit: int = 100,
    user_limit: int = 100,
) -> CostBreakdownResponse:
    """Add up billable trial spend for the admin cost dashboard."""
    now = datetime.now(timezone.utc)
    since = None if window_days is None else now - timedelta(days=window_days)

    bucket = _series_bucket(window_days)
    series_by_agent, series_by_model, series_by_user = await _cost_time_series(
        session, since=since, bucket=bucket
    )

    # Shared expression objects: reused verbatim in SELECT and GROUP BY so the
    # JSON-key bind params match (two inline copies bind as distinct params and
    # Postgres then rejects the GROUP BY).
    gh_id_col = TaskModel.tags["github_id"].astext
    gh_user_col = TaskModel.tags["github_username"].astext
    detail_query = (
        select(
            TrialModel.experiment_id.label("experiment_id"),
            ExperimentModel.name.label("exp_name"),
            ExperimentModel.org_id.label("exp_org_id"),
            TrialModel.org_id.label("trial_org_id"),
            ExperimentModel.owner_user_id.label("owner_user_id"),
            ExperimentModel.owner.label("exp_owner"),
            TrialModel.billed_user_id.label("billed_user_id"),
            gh_id_col.label("gh_id"),
            gh_user_col.label("gh_user"),
            TaskModel.created_by_user_id.label("submitter"),
            ExperimentModel.created_at.label("exp_created_at"),
            ExperimentModel.last_activity_at.label("exp_last_activity_at"),
            TrialModel.model.label("model"),
            TrialModel.provider.label("provider"),
            func.count(TrialModel.id).label("trial_count"),
            func.coalesce(func.sum(TrialModel.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TrialModel.cache_tokens), 0).label("cache_tokens"),
            func.coalesce(func.sum(TrialModel.output_tokens), 0).label("output_tokens"),
            *settled_cost_columns(),
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        # LEFT join so a soft-deleted task yields NULL tags (the spend still
        # counts, attributed by the fallback) rather than dropping the trial.
        .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
        .group_by(
            TrialModel.experiment_id,
            ExperimentModel.name,
            ExperimentModel.org_id,
            ExperimentModel.owner_user_id,
            ExperimentModel.owner,
            TrialModel.org_id,
            TrialModel.billed_user_id,
            gh_id_col,
            gh_user_col,
            TaskModel.created_by_user_id,
            ExperimentModel.created_at,
            ExperimentModel.last_activity_at,
            TrialModel.model,
            TrialModel.provider,
        )
    )
    detail_query = detail_query.where(
        _real_spend_filter(), TrialModel.finished_at.isnot(None)
    )
    if since is not None:
        detail_query = detail_query.where(TrialModel.finished_at >= since)

    rows = (await session.execute(detail_query)).all()

    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    experiments: dict[str, dict[str, Any]] = {}
    by_user: dict[tuple[str | None, str], dict[str, Any]] = {}

    total_trials = 0
    total_input = 0
    total_cache = 0
    total_output = 0
    total_native = 0.0
    total_estimated = 0.0

    for row in rows:
        owner_user_id = _normalize_owner_user_id(row.owner_user_id)
        user_key, user_real_id, user_label = _spend_identity(
            row.billed_user_id, row.gh_id, row.gh_user, row.submitter
        )
        row_billed = row.billed_user_id is not None
        model = _model_label(row.model)
        provider = _provider_label(row.provider)
        trial_count = int(row.trial_count or 0)
        input_tokens = int(row.input_tokens or 0)
        cache_tokens = int(row.cache_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        native, estimated = settled_cost_parts(row)
        cost = native + estimated

        total_trials += trial_count
        total_input += input_tokens
        total_cache += cache_tokens
        total_output += output_tokens
        total_native += native
        total_estimated += estimated

        _accumulate_model(
            by_model,
            model=model,
            provider=provider,
            trial_count=trial_count,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cost_estimated_usd=estimated,
        )

        exp = experiments.get(row.experiment_id)
        if exp is None:
            exp = experiments[row.experiment_id] = {
                "experiment_id": row.experiment_id,
                "name": row.exp_name,
                "org_id": row.exp_org_id,
                "owner_user_id": owner_user_id,
                "owner": row.exp_owner,
                "created_at": row.exp_created_at,
                "last_activity_at": row.exp_last_activity_at,
                "trial_count": 0,
                "input_tokens": 0,
                "cache_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "cost_estimated_usd": 0.0,
                "models": {},
            }
        exp["trial_count"] += trial_count
        exp["input_tokens"] += input_tokens
        exp["cache_tokens"] += cache_tokens
        exp["output_tokens"] += output_tokens
        exp["cost_usd"] += cost
        exp["cost_estimated_usd"] += estimated
        _accumulate_model(
            exp["models"],
            model=model,
            provider=provider,
            trial_count=trial_count,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cost_estimated_usd=estimated,
        )

        row_key = (row.trial_org_id, user_key)
        user = by_user.get(row_key)
        if user is None:
            user = by_user[row_key] = {
                "key": user_key,
                "real_user_id": user_real_id,
                "label": user_label,
                # A user key can gather both billed and unbilled groups (someone
                # billed while active, then the submitter fallback for a later
                # trial after they were offboarded, or pre-billing spend that was
                # never stamped). ``all_billed`` tracks whether EVERY group is
                # billed so the row can flag unbilled spend; it no longer gates
                # linkability (a real user's row links regardless).
                "all_billed": row_billed,
                "org_id": row.trial_org_id,
                "trial_count": 0,
                "input_tokens": 0,
                "cache_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "cost_estimated_usd": 0.0,
                "experiment_ids": set(),
                "models": {},
            }
        else:
            user["all_billed"] = user["all_billed"] and row_billed
        user["trial_count"] += trial_count
        user["input_tokens"] += input_tokens
        user["cache_tokens"] += cache_tokens
        user["output_tokens"] += output_tokens
        user["cost_usd"] += cost
        user["cost_estimated_usd"] += estimated
        user["experiment_ids"].add(row.experiment_id)
        _accumulate_model(
            user["models"],
            model=model,
            provider=provider,
            trial_count=trial_count,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cost_estimated_usd=estimated,
        )

    if since is None:
        prev_by_user: dict[tuple[str | None, str], float] = {}
        prev_window_cost: float | None = None
    else:
        prev_by_user, prev_window_cost = await _prev_window_costs(
            session,
            prev_start=now - 2 * timedelta(days=window_days),
            prev_end=since,
        )

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_org_ids = (
        await session.scalars(
            select(TrialModel.org_id)
            .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
            .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
            .where(
                _real_spend_filter(),
                TrialModel.finished_at >= month_start,
            )
            .distinct()
        )
    ).all()
    month_limits_by_org = {
        org_id: await get_effective_org_limit(session, org_id)
        for org_id in month_org_ids
    }
    budgeted_month_org_ids = {
        org_id for org_id, limit in month_limits_by_org.items() if limit is not None
    }
    month_limits = [
        limit for limit in month_limits_by_org.values() if limit is not None
    ]
    month_budget = float(sum(month_limits)) if month_limits else None
    month_cost = await _billed_cost_since(
        session,
        since=month_start,
        org_ids=budgeted_month_org_ids if month_budget is not None else None,
    )

    quota_start = quota_window_start(now)
    quota_spent = await sum_cost_usd_by_org_user_all_orgs(session, quota_start)
    quota_limits = await effective_limits_by_org_user_all_orgs(session)
    inflight_counts = await inflight_trial_count_by_org_user_all_orgs(session)

    user_rows = sorted(by_user.values(), key=lambda u: u["cost_usd"], reverse=True)[
        :user_limit
    ]
    by_user_out = [
        CostUserBreakdown(
            key=u["key"],
            # Link whenever the row resolves to a real oddish user, even if it
            # holds unbilled spend: "unbilled" doesn't mean "not a real user"
            # (e.g. pre-billing spend). GitHub-handle / Unattributed fallback
            # rows have no real_user_id and stay non-clickable.
            owner_user_id=u["real_user_id"],
            # Flag any row carrying unbilled spend. On a linkable row this warns
            # the drilldown (billed-only) may total less than this row.
            has_unbilled_spend=not u["all_billed"],
            label=u["label"],
            org_id=u["org_id"],
            trial_count=int(u["trial_count"]),
            experiment_count=len(u["experiment_ids"]),
            input_tokens=int(u["input_tokens"]),
            cache_tokens=int(u["cache_tokens"]),
            output_tokens=int(u["output_tokens"]),
            cost_usd=round(float(u["cost_usd"]), 4),
            cost_estimated_usd=round(float(u["cost_estimated_usd"]), 4),
            models=_model_breakdowns(u["models"], limit=_MAX_MODELS_PER_USER),
            prev_cost_usd=(
                None
                if since is None
                else round(prev_by_user.get((u["org_id"], u["key"]), 0.0), 4)
            ),
            inflight_trial_count=(
                inflight_counts.get((u["org_id"], u["real_user_id"]), 0)
                if u["all_billed"] and u["real_user_id"]
                else 0
            ),
            quota_spent_usd=(
                float(quota_spent.get((u["org_id"], u["real_user_id"]), 0.0))
                if u["all_billed"] and u["real_user_id"]
                else None
            ),
            quota_limit_usd=(
                float(
                    quota_limits.get(
                        (u["org_id"], u["real_user_id"]),
                        settings.default_daily_quota_usd,
                    )
                )
                if u["all_billed"] and u["real_user_id"]
                else None
            ),
        )
        for u in user_rows
    ]

    experiment_rows = sorted(
        experiments.values(), key=lambda e: e["cost_usd"], reverse=True
    )[:experiment_limit]
    task_authors = await _primary_task_authors(
        session, [str(e["experiment_id"]) for e in experiment_rows]
    )
    experiments_out = [
        CostExperimentBreakdown(
            experiment_id=str(e["experiment_id"]),
            name=e["name"],
            org_id=e["org_id"],
            owner_user_id=e["owner_user_id"],
            owner_label=_clean_author(e["owner"])
            or task_authors.get(str(e["experiment_id"])),
            created_at=e["created_at"],
            last_activity_at=e["last_activity_at"],
            trial_count=int(e["trial_count"]),
            input_tokens=int(e["input_tokens"]),
            cache_tokens=int(e["cache_tokens"]),
            output_tokens=int(e["output_tokens"]),
            cost_usd=round(float(e["cost_usd"]), 4),
            cost_estimated_usd=round(float(e["cost_estimated_usd"]), 4),
            models=_model_breakdowns(e["models"], limit=_MAX_MODELS_PER_EXPERIMENT),
        )
        for e in experiment_rows
    ]

    totals = CostTotals(
        window_days=window_days,
        trial_count=total_trials,
        experiment_count=len(experiments),
        # Count distinct real users only -- the GitHub-handle and Unattributed
        # fallback buckets aren't oddish users, and a user billed in more than
        # one org gets one row per org but is still one user.
        user_count=len(
            {u["real_user_id"] for u in by_user.values() if u["real_user_id"]}
        ),
        input_tokens=total_input,
        cache_tokens=total_cache,
        output_tokens=total_output,
        cost_usd=round(total_native + total_estimated, 4),
        cost_native_usd=round(total_native, 4),
        cost_estimated_usd=round(total_estimated, 4),
        prev_cost_usd=prev_window_cost,
        month_cost_usd=month_cost,
        month_budget_usd=month_budget,
    )

    return CostBreakdownResponse(
        window_days=window_days,
        bucket=bucket,
        series_by_agent=series_by_agent,
        series_by_model=series_by_model,
        series_by_user=series_by_user,
        totals=totals,
        by_user=by_user_out,
        by_model=_model_breakdowns(by_model),
        experiments=experiments_out,
        timestamp=now.isoformat(),
    )


class CostTaskBreakdown(BaseModel):
    task_id: str
    task_name: str | None
    trial_count: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_estimated_usd: float
    models: list[CostModelBreakdown]


class UserCostExperimentBreakdown(BaseModel):
    experiment_id: str
    name: str | None
    trial_count: int
    cost_usd: float
    models: list[CostModelBreakdown]


class UserCostTotals(BaseModel):
    window_days: int | None
    trial_count: int
    task_count: int
    experiment_count: int = 0
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float
    cost_native_usd: float
    cost_estimated_usd: float


class UserCostBreakdownResponse(BaseModel):
    billed_user_id: str
    org_id: str | None
    name: str | None = None
    email: str | None = None
    github_username: str | None = None
    window_days: int | None
    bucket: str
    series_by_model: CostSeries
    totals: UserCostTotals
    tasks: list[CostTaskBreakdown]
    experiments: list[UserCostExperimentBreakdown] = []
    timestamp: str


async def get_user_cost_breakdown_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    billed_user_id: str,
    window_days: int | None = 7,
    task_limit: int = 100,
) -> UserCostBreakdownResponse:
    """One user's settled billed spend: totals, per-task rollup, by-model series."""
    now = datetime.now(timezone.utc)
    since = None if window_days is None else now - timedelta(days=window_days)
    bucket = _series_bucket(window_days)

    filters = [
        TrialModel.org_id == org_id,
        TrialModel.billed_user_id == billed_user_id,
        TrialModel.finished_at.isnot(None),
        first_party_spend_filter(),
    ]
    if since is not None:
        filters.append(TrialModel.finished_at >= since)

    bucket_col = func.date_trunc(bucket, TrialModel.finished_at)

    detail_query = (
        select(
            TrialModel.task_id.label("task_id"),
            TaskModel.name.label("task_name"),
            TrialModel.model.label("model"),
            TrialModel.provider.label("provider"),
            *settled_cost_columns(),
            func.coalesce(func.sum(TrialModel.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TrialModel.cache_tokens), 0).label("cache_tokens"),
            func.coalesce(func.sum(TrialModel.output_tokens), 0).label("output_tokens"),
            func.count(TrialModel.id).label("trial_count"),
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id, isouter=True)
        .where(*filters)
        .group_by(
            TrialModel.task_id,
            TaskModel.name,
            TrialModel.model,
            TrialModel.provider,
        )
        .execution_options(include_deleted=True)
    )

    series_query = (
        select(
            bucket_col.label("bucket"),
            TrialModel.model.label("model"),
            *settled_cost_columns(),
            func.count(TrialModel.id).label("trial_count"),
        )
        .where(*filters)
        .group_by(bucket_col, TrialModel.model)
        .execution_options(include_deleted=True)
    )

    detail_rows = (await session.execute(detail_query)).all()
    series_rows = (await session.execute(series_query)).all()

    model_per_bucket: dict[datetime, dict[str, float]] = {}
    model_totals: dict[str, float] = {}
    trials_per_bucket: dict[datetime, int] = {}

    for row in series_rows:
        model = _model_label(row.model)
        cost = settled_cost_from_row(row)
        bstart = row.bucket
        slot = model_per_bucket.setdefault(bstart, {})
        slot[model] = slot.get(model, 0.0) + cost
        model_totals[model] = model_totals.get(model, 0.0) + cost
        trials_per_bucket[bstart] = trials_per_bucket.get(bstart, 0) + int(
            row.trial_count or 0
        )

    tasks: dict[str, dict[str, Any]] = {}

    total_trials = 0
    total_input = 0
    total_cache = 0
    total_output = 0
    total_native = 0.0
    total_estimated = 0.0

    for row in detail_rows:
        model = _model_label(row.model)
        provider = _provider_label(row.provider)
        trial_count = int(row.trial_count or 0)
        input_tokens = int(row.input_tokens or 0)
        cache_tokens = int(row.cache_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        native, estimated = settled_cost_parts(row)
        cost = native + estimated

        total_trials += trial_count
        total_input += input_tokens
        total_cache += cache_tokens
        total_output += output_tokens
        total_native += native
        total_estimated += estimated

        task = tasks.get(row.task_id)
        if task is None:
            task = tasks[row.task_id] = {
                "task_id": row.task_id,
                "task_name": row.task_name,
                "trial_count": 0,
                "input_tokens": 0,
                "cache_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "cost_estimated_usd": 0.0,
                "models": {},
            }
        task["trial_count"] += trial_count
        task["input_tokens"] += input_tokens
        task["cache_tokens"] += cache_tokens
        task["output_tokens"] += output_tokens
        task["cost_usd"] += cost
        task["cost_estimated_usd"] += estimated
        _accumulate_model(
            task["models"],
            model=model,
            provider=provider,
            trial_count=trial_count,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cost_estimated_usd=estimated,
        )

    bucket_starts = sorted(trials_per_bucket.keys())
    series_by_model = _build_dimension_series(
        "model",
        bucket_starts=bucket_starts,
        per_bucket=model_per_bucket,
        totals=model_totals,
        trials_per_bucket=trials_per_bucket,
        labels={},
    )

    exp_query = (
        select(
            TrialModel.experiment_id.label("experiment_id"),
            ExperimentModel.name.label("exp_name"),
            TrialModel.model.label("model"),
            TrialModel.provider.label("provider"),
            *settled_cost_columns(),
            func.count(TrialModel.id).label("trial_count"),
        )
        .join(
            ExperimentModel,
            ExperimentModel.id == TrialModel.experiment_id,
            isouter=True,
        )
        .where(*filters)
        .group_by(
            TrialModel.experiment_id,
            ExperimentModel.name,
            TrialModel.model,
            TrialModel.provider,
        )
        .execution_options(include_deleted=True)
    )
    exp_rows = (await session.execute(exp_query)).all()

    exps: dict[str, dict[str, Any]] = {}
    for row in exp_rows:
        model = _model_label(row.model)
        provider = _provider_label(row.provider)
        trial_count = int(row.trial_count or 0)
        native, estimated = settled_cost_parts(row)
        cost = native + estimated
        exp = exps.get(row.experiment_id)
        if exp is None:
            exp = exps[row.experiment_id] = {
                "experiment_id": row.experiment_id,
                "name": row.exp_name,
                "trial_count": 0,
                "cost_usd": 0.0,
                "models": {},
            }
        exp["trial_count"] += trial_count
        exp["cost_usd"] += cost
        _accumulate_model(
            exp["models"],
            model=model,
            provider=provider,
            trial_count=trial_count,
            input_tokens=0,
            cache_tokens=0,
            output_tokens=0,
            cost_usd=cost,
            cost_estimated_usd=estimated,
        )

    experiments_out = [
        UserCostExperimentBreakdown(
            experiment_id=str(e["experiment_id"]),
            name=e["name"],
            trial_count=int(e["trial_count"]),
            cost_usd=round(float(e["cost_usd"]), 4),
            models=_model_breakdowns(e["models"], limit=_MAX_MODELS_PER_EXPERIMENT),
        )
        for e in sorted(exps.values(), key=lambda e: e["cost_usd"], reverse=True)[
            :task_limit
        ]
    ]

    task_rows = sorted(tasks.values(), key=lambda t: t["cost_usd"], reverse=True)[
        :task_limit
    ]
    tasks_out = [
        CostTaskBreakdown(
            task_id=str(t["task_id"]),
            task_name=t["task_name"],
            trial_count=int(t["trial_count"]),
            input_tokens=int(t["input_tokens"]),
            cache_tokens=int(t["cache_tokens"]),
            output_tokens=int(t["output_tokens"]),
            cost_usd=round(float(t["cost_usd"]), 4),
            cost_estimated_usd=round(float(t["cost_estimated_usd"]), 4),
            models=_model_breakdowns(t["models"]),
        )
        for t in task_rows
    ]

    totals = UserCostTotals(
        window_days=window_days,
        trial_count=total_trials,
        task_count=len(tasks),
        experiment_count=len(exps),
        input_tokens=total_input,
        cache_tokens=total_cache,
        output_tokens=total_output,
        cost_usd=round(total_native + total_estimated, 4),
        cost_native_usd=round(total_native, 4),
        cost_estimated_usd=round(total_estimated, 4),
    )

    return UserCostBreakdownResponse(
        billed_user_id=billed_user_id,
        org_id=org_id,
        window_days=window_days,
        bucket=bucket,
        series_by_model=series_by_model,
        totals=totals,
        tasks=tasks_out,
        experiments=experiments_out,
        timestamp=now.isoformat(),
    )
