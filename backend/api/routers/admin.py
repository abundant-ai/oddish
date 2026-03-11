"""
Admin endpoints for internal system monitoring.

Provides visibility into:
- Queue slot leases (worker concurrency tracking)
- PGQueuer job queue state (raw queue internals)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text

from auth import AuthContext, require_admin
from oddish.config import settings
from oddish.db import get_session, utcnow

router = APIRouter(prefix="/admin", tags=["Admin"])


# =============================================================================
# Response Models
# =============================================================================


class QueueSlot(BaseModel):
    """A single queue-key slot lease."""

    queue_key: str
    slot: int
    locked_by: str | None
    locked_until: datetime | None
    is_active: bool  # True if currently locked


class QueueSlotSummary(BaseModel):
    """Summary of slots for a single queue key."""

    queue_key: str
    total_slots: int
    active_slots: int
    slots: list[QueueSlot]


class QueueSlotsResponse(BaseModel):
    """Response for queue slot endpoint."""

    queue_keys: list[QueueSlotSummary]
    total_slots: int
    total_active: int
    timestamp: str


class PGQueuerJob(BaseModel):
    """A single job from the pgqueuer table."""

    id: int
    priority: int
    created: datetime | None
    updated: datetime | None
    status: str
    entrypoint: str
    payload: dict | None


class PGQueuerStats(BaseModel):
    """Statistics for pgqueuer jobs."""

    total: int
    by_status: dict[str, int]
    by_entrypoint: dict[str, dict[str, int]]


class PGQueuerResponse(BaseModel):
    """Response for pgqueuer endpoint."""

    jobs: list[PGQueuerJob]
    stats: PGQueuerStats
    page: int
    page_size: int
    has_more: bool
    timestamp: str


class OrphanedTrialSample(BaseModel):
    trial_id: str
    task_id: str
    queue_key: str
    status: str
    issue: str
    harbor_stage: str | None
    current_pgqueuer_job_id: int | None
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
    queued_without_job: int
    running_without_picked_job: int
    running_stale_heartbeat: int
    picked_without_active_slot: int
    active_tasks_without_active_trials: int


class OrphanedStateResponse(BaseModel):
    counts: OrphanedStateCounts
    trial_samples: list[OrphanedTrialSample]
    task_samples: list[OrphanedTaskSample]
    stale_after_minutes: int
    timestamp: str


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/slots", response_model=QueueSlotsResponse)
async def get_queue_slots(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueSlotsResponse:
    """
    Get current state of queue-key slot leases.

    Shows which workers have acquired slots and when they expire.
    Requires admin role.
    """
    now = utcnow()

    async with get_session() as session:
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

    # Group by canonical queue key
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
        if queue_key not in queue_map:
            queue_map[queue_key] = []
        queue_map[queue_key].append(slot)

    # Build summaries
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


@router.get("/pgqueuer", response_model=PGQueuerResponse)
async def get_pgqueuer_jobs(
    auth: Annotated[AuthContext, Depends(require_admin)],
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    status: str | None = Query(
        None, description="Filter by status (queued, picked, etc)"
    ),
    entrypoint: str | None = Query(None, description="Filter by queue entrypoint"),
) -> PGQueuerResponse:
    """
    Get raw pgqueuer job queue state.

    Shows the internal job queue used by workers. This is the source of truth
    for queue state before jobs become trials.

    Requires admin role.
    """
    import json

    now = utcnow()
    offset = (page - 1) * page_size

    async with get_session() as session:
        # Build filters
        filters = []
        params: dict = {"limit": page_size + 1, "offset": offset}

        if status:
            filters.append("status::text = :status")
            params["status"] = status
        if entrypoint:
            filters.append("entrypoint = :entrypoint")
            params["entrypoint"] = entrypoint

        where_clause = " AND ".join(filters) if filters else "1=1"

        # Fetch jobs (one extra to check if there are more)
        result = await session.execute(
            text(
                f"""
                SELECT id, priority, created, updated, status::text, entrypoint, payload
                FROM pgqueuer
                WHERE {where_clause}
                ORDER BY created DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        rows = result.all()

        # Parse jobs
        jobs = []
        for row in rows[:page_size]:
            payload_raw = row[6]
            payload_dict = None
            if payload_raw:
                try:
                    if isinstance(payload_raw, bytes):
                        payload_dict = json.loads(
                            payload_raw.decode("utf-8", errors="replace")
                        )
                    elif isinstance(payload_raw, str):
                        payload_dict = json.loads(payload_raw)
                    else:
                        payload_dict = payload_raw
                except Exception:
                    payload_dict = {"raw": str(payload_raw)[:200]}

            jobs.append(
                PGQueuerJob(
                    id=row[0],
                    priority=row[1],
                    created=row[2],
                    updated=row[3],
                    status=row[4],
                    entrypoint=row[5],
                    payload=payload_dict,
                )
            )

        has_more = len(rows) > page_size

        # Get stats (unfiltered for overview)
        stats_result = await session.execute(
            text(
                """
                SELECT
                    status::text,
                    entrypoint,
                    COUNT(*) as count
                FROM pgqueuer
                GROUP BY status, entrypoint
                """
            )
        )
        stats_rows = stats_result.all()

        by_status: dict[str, int] = {}
        by_entrypoint: dict[str, dict[str, int]] = {}

        for status_val, ep, count in stats_rows:
            by_status[status_val] = by_status.get(status_val, 0) + count
            if ep not in by_entrypoint:
                by_entrypoint[ep] = {}
            by_entrypoint[ep][status_val] = count

        total = sum(by_status.values())

    return PGQueuerResponse(
        jobs=jobs,
        stats=PGQueuerStats(
            total=total,
            by_status=by_status,
            by_entrypoint=by_entrypoint,
        ),
        page=page,
        page_size=page_size,
        has_more=has_more,
        timestamp=now.isoformat(),
    )


@router.get("/orphaned-state", response_model=OrphanedStateResponse)
async def get_orphaned_state(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stale_after_minutes: int = Query(10, ge=1, le=240),
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state using explicit trial execution metadata."""
    now = utcnow()

    async with get_session() as session:
        counts_row = (
            await session.execute(
                text(
                    """
                    WITH queued_jobs AS (
                        SELECT convert_from(payload, 'utf8')::jsonb->>'trial_id' AS trial_id
                        FROM pgqueuer
                        WHERE status = 'queued'
                    ),
                    picked_jobs AS (
                        SELECT id, entrypoint
                        FROM pgqueuer
                        WHERE status = 'picked'
                    ),
                    active_slots AS (
                        SELECT queue_key,
                               COUNT(*) FILTER (
                                   WHERE locked_by IS NOT NULL
                                     AND locked_until IS NOT NULL
                                     AND locked_until > NOW()
                               ) AS active_slots
                        FROM queue_slots
                        GROUP BY queue_key
                    ),
                    stale_tasks AS (
                        SELECT t.id
                        FROM tasks t
                        LEFT JOIN trials tr ON tr.task_id = t.id
                        WHERE t.status IN ('RUNNING', 'ANALYZING', 'VERDICT_PENDING')
                        GROUP BY t.id
                        HAVING COUNT(*) FILTER (
                            WHERE tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                        ) = 0
                    )
                    SELECT
                        (
                            SELECT COUNT(*)
                            FROM trials t
                            WHERE t.status = 'QUEUED'
                              AND NOT EXISTS (
                                  SELECT 1 FROM queued_jobs q WHERE q.trial_id = t.id
                              )
                        ) AS queued_without_job,
                        (
                            SELECT COUNT(*)
                            FROM trials t
                            WHERE t.status = 'RUNNING'
                              AND (
                                  t.current_pgqueuer_job_id IS NULL
                                  OR NOT EXISTS (
                                      SELECT 1
                                      FROM picked_jobs p
                                      WHERE p.id = t.current_pgqueuer_job_id
                                  )
                              )
                        ) AS running_without_picked_job,
                        (
                            SELECT COUNT(*)
                            FROM trials t
                            WHERE t.status = 'RUNNING'
                              AND (
                                  t.heartbeat_at IS NULL
                                  OR t.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                              )
                        ) AS running_stale_heartbeat,
                        (
                            SELECT COUNT(*)
                            FROM pgqueuer p
                            LEFT JOIN active_slots s ON s.queue_key = p.entrypoint
                            WHERE p.status = 'picked'
                              AND COALESCE(s.active_slots, 0) = 0
                        ) AS picked_without_active_slot,
                        (
                            SELECT COUNT(*) FROM stale_tasks
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
                    WITH queued_jobs AS (
                        SELECT convert_from(payload, 'utf8')::jsonb->>'trial_id' AS trial_id
                        FROM pgqueuer
                        WHERE status = 'queued'
                    ),
                    picked_jobs AS (
                        SELECT id
                        FROM pgqueuer
                        WHERE status = 'picked'
                    )
                    SELECT *
                    FROM (
                        SELECT
                            t.id AS trial_id,
                            t.task_id,
                            t.queue_key,
                            t.status::text AS status,
                            'queued_without_job'::text AS issue,
                            t.harbor_stage,
                            t.current_pgqueuer_job_id,
                            t.current_worker_id,
                            t.current_queue_slot,
                            t.claimed_at,
                            t.heartbeat_at,
                            t.updated_at
                        FROM trials t
                        WHERE t.status = 'QUEUED'
                          AND NOT EXISTS (
                              SELECT 1 FROM queued_jobs q WHERE q.trial_id = t.id
                          )

                        UNION ALL

                        SELECT
                            t.id AS trial_id,
                            t.task_id,
                            t.queue_key,
                            t.status::text AS status,
                            'running_without_picked_job'::text AS issue,
                            t.harbor_stage,
                            t.current_pgqueuer_job_id,
                            t.current_worker_id,
                            t.current_queue_slot,
                            t.claimed_at,
                            t.heartbeat_at,
                            t.updated_at
                        FROM trials t
                        WHERE t.status = 'RUNNING'
                          AND (
                              t.current_pgqueuer_job_id IS NULL
                              OR NOT EXISTS (
                                  SELECT 1 FROM picked_jobs p
                                  WHERE p.id = t.current_pgqueuer_job_id
                              )
                          )

                        UNION ALL

                        SELECT
                            t.id AS trial_id,
                            t.task_id,
                            t.queue_key,
                            t.status::text AS status,
                            'running_stale_heartbeat'::text AS issue,
                            t.harbor_stage,
                            t.current_pgqueuer_job_id,
                            t.current_worker_id,
                            t.current_queue_slot,
                            t.claimed_at,
                            t.heartbeat_at,
                            t.updated_at
                        FROM trials t
                        WHERE t.status = 'RUNNING'
                          AND (
                              t.heartbeat_at IS NULL
                              OR t.heartbeat_at < NOW() - make_interval(mins => :stale_after_minutes)
                          )
                    ) trial_issues
                    ORDER BY updated_at ASC NULLS FIRST
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
                    WITH stale_tasks AS (
                        SELECT
                            t.id AS task_id,
                            t.status::text AS status,
                            t.run_analysis,
                            t.verdict_status::text AS verdict_status,
                            t.updated_at
                        FROM tasks t
                        LEFT JOIN trials tr ON tr.task_id = t.id
                        WHERE t.status IN ('RUNNING', 'ANALYZING', 'VERDICT_PENDING')
                        GROUP BY t.id, t.status, t.run_analysis, t.verdict_status, t.updated_at
                        HAVING COUNT(*) FILTER (
                            WHERE tr.status IN ('QUEUED', 'RUNNING', 'RETRYING')
                        ) = 0
                    )
                    SELECT
                        task_id,
                        status,
                        run_analysis,
                        verdict_status,
                        'active_task_without_active_trials'::text AS issue,
                        updated_at
                    FROM stale_tasks
                    ORDER BY updated_at ASC NULLS FIRST
                    LIMIT 20
                    """
                )
            )
        ).all()

    return OrphanedStateResponse(
        counts=OrphanedStateCounts(
            queued_without_job=int(counts_row.queued_without_job or 0),
            running_without_picked_job=int(counts_row.running_without_picked_job or 0),
            running_stale_heartbeat=int(counts_row.running_stale_heartbeat or 0),
            picked_without_active_slot=int(counts_row.picked_without_active_slot or 0),
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
                current_pgqueuer_job_id=row.current_pgqueuer_job_id,
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
