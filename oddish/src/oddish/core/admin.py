"""Admin diagnostic queries for queue slots, status, and orphaned state."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import utcnow


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
    queue_key: str
    queued: int
    running: int


class QueueStatusResponse(BaseModel):
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
    """Get queue status from the trials/tasks tables."""
    now = utcnow()

    trial_rows = (
        await session.execute(
            text(
                """
                SELECT
                    queue_key,
                    COUNT(*) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')) AS queued,
                    COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running
                FROM trials
                WHERE status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
                GROUP BY queue_key
                ORDER BY queue_key
                """
            )
        )
    ).all()

    analysis_row = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE analysis_status::text = 'QUEUED') AS queued,
                    COUNT(*) FILTER (WHERE analysis_status::text = 'RUNNING') AS running
                FROM trials WHERE analysis_status IS NOT NULL
                """
            )
        )
    ).one()

    verdict_row = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE verdict_status::text = 'QUEUED') AS queued,
                    COUNT(*) FILTER (WHERE verdict_status::text = 'RUNNING') AS running
                FROM tasks WHERE verdict_status IS NOT NULL
                """
            )
        )
    ).one()

    return QueueStatusResponse(
        trial_queues=[
            QueueStatusEntry(
                queue_key=settings.normalize_queue_key(row[0]),
                queued=int(row[1] or 0),
                running=int(row[2] or 0),
            )
            for row in trial_rows
        ],
        analysis_queued=int(analysis_row[0] or 0),
        analysis_running=int(analysis_row[1] or 0),
        verdict_queued=int(verdict_row[0] or 0),
        verdict_running=int(verdict_row[1] or 0),
        timestamp=now.isoformat(),
    )


async def get_orphaned_state_core(
    session: AsyncSession,
    *,
    stale_after_minutes: int = 15,
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state."""
    now = utcnow()

    counts_row = (
        await session.execute(
            text(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM trials t
                        WHERE t.status::text = 'RUNNING'
                          AND (
                              t.heartbeat_at IS NULL
                              OR t.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                          )
                    ) AS running_stale_heartbeat,
                    (
                        SELECT COUNT(*)
                        FROM tasks t
                        WHERE (
                            t.status = 'RUNNING'
                            AND NOT EXISTS (
                                SELECT 1 FROM trials tr
                                WHERE tr.task_id = t.id
                                  AND tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                            )
                        ) OR (
                            t.status = 'ANALYZING'
                            AND NOT EXISTS (
                                SELECT 1 FROM trials tr
                                WHERE tr.task_id = t.id
                                  AND tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                            )
                        ) OR (
                            t.status = 'VERDICT_PENDING'
                            AND (t.verdict_status IS NULL
                                 OR t.verdict_status::text NOT IN ('QUEUED', 'RUNNING'))
                        )
                    ) AS active_tasks_without_active_trials
                """
            ),
            {"stale_after_minutes": stale_after_minutes},
        )
    ).one()

    trial_rows = (
        await session.execute(
            text(
                """
                SELECT
                    t.id AS trial_id,
                    t.task_id,
                    t.queue_key,
                    t.status::text AS status,
                    'running_stale_heartbeat'::text AS issue,
                    t.harbor_stage,
                    t.current_worker_id,
                    t.current_queue_slot,
                    t.claimed_at,
                    t.heartbeat_at,
                    t.updated_at
                FROM trials t
                WHERE t.status::text = 'RUNNING'
                  AND (
                      t.heartbeat_at IS NULL
                      OR t.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                  )
                ORDER BY t.updated_at ASC NULLS FIRST
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
                WHERE (
                    t.status = 'RUNNING'
                    AND NOT EXISTS (
                        SELECT 1 FROM trials tr
                        WHERE tr.task_id = t.id
                          AND tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                    )
                ) OR (
                    t.status = 'ANALYZING'
                    AND NOT EXISTS (
                        SELECT 1 FROM trials tr
                        WHERE tr.task_id = t.id
                          AND tr.analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')
                    )
                ) OR (
                    t.status = 'VERDICT_PENDING'
                    AND (t.verdict_status IS NULL
                         OR t.verdict_status::text NOT IN ('QUEUED', 'RUNNING'))
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
