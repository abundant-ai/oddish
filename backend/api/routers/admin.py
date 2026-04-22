"""Admin endpoints — auth wrapper over oddish core diagnostics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from auth import AuthContext, require_admin
from oddish.core.admin import (
    QueueSlotsResponse,
    QueueStatusResponse,
    OrphanedStateResponse,
    WorkerJobsResponse,
    get_queue_slots_core,
    get_queue_status_core,
    get_orphaned_state_core,
    get_worker_jobs_admin_core,
)
from oddish.db import TaskModel, TaskVersionModel, get_session
from oddish.queue import enqueue_task_expand_worker_job

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/slots", response_model=QueueSlotsResponse)
async def get_queue_slots(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueSlotsResponse:
    """Get current state of queue-key slot leases."""
    async with get_session() as session:
        return await get_queue_slots_core(session)


@router.get("/queue-status", response_model=QueueStatusResponse)
async def get_queue_status(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueStatusResponse:
    """Get queue status from the trials/tasks tables (the source of truth)."""
    async with get_session() as session:
        return await get_queue_status_core(session)


@router.get("/orphaned-state", response_model=OrphanedStateResponse)
async def get_orphaned_state(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stale_after_minutes: int = Query(15, ge=1, le=240),
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state."""
    async with get_session() as session:
        return await get_orphaned_state_core(
            session, stale_after_minutes=stale_after_minutes
        )


@router.get("/worker-jobs", response_model=WorkerJobsResponse)
async def get_worker_jobs(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stale_after_minutes: int = Query(15, ge=1, le=240),
    sample_limit: int = Query(25, ge=1, le=100),
) -> WorkerJobsResponse:
    """Summarize the unified ``worker_jobs`` queue by (kind, status).

    Powers the "Worker Jobs" admin panel which treats each kind (TRIAL,
    ANALYSIS, VERDICT, ...) as an independently queued agent job.
    """
    async with get_session() as session:
        return await get_worker_jobs_admin_core(
            session,
            stale_after_minutes=stale_after_minutes,
            sample_limit=sample_limit,
        )


class ExpandBackfillResponse(BaseModel):
    """Response for a single backfill batch.

    - ``enqueued`` is the number of ``TASK_EXPAND`` jobs scheduled by
      this call.
    - ``pending_total`` is the full-table count of versions matching
      the current filters (``expanded_at IS NULL AND task_s3_key IS
      NOT NULL``, plus any ``task_id`` / ``org_id`` filter), measured
      before this call's inserts.  ``pending_total - enqueued`` tells
      the operator how many more calls are needed to drain the
      backlog; re-run once the workers have chewed through the
      current batch and ``pending_total`` drops accordingly.
    """

    enqueued: int
    pending_total: int


@router.post("/tasks/expand-backfill", response_model=ExpandBackfillResponse)
async def backfill_task_expansions(
    auth: Annotated[AuthContext, Depends(require_admin)],
    task_id: str | None = Query(None, description="Restrict to one task_id"),
    org_id: str | None = Query(None, description="Restrict to one org_id"),
    limit: int = Query(500, ge=1, le=5000),
) -> ExpandBackfillResponse:
    """Enqueue ``TASK_EXPAND`` jobs for task versions that haven't been expanded.

    The handler is idempotent (keyed on the archive's etag via
    ``.oddish-manifest.json``) so callers can re-run the backfill
    without duplicating work.

    When ``org_id`` is supplied, the filter is pushed into SQL (joined
    on ``task.org_id``) rather than applied after the fetch, so a
    single-org backfill doesn't drag unrelated versions into memory.
    """
    filters = [
        TaskVersionModel.expanded_at.is_(None),
        TaskVersionModel.task_s3_key.isnot(None),
    ]
    if task_id:
        filters.append(TaskVersionModel.task_id == task_id)
    if org_id:
        filters.append(TaskModel.org_id == org_id)

    def _apply_join(stmt):
        if org_id:
            return stmt.join(
                TaskModel, TaskModel.id == TaskVersionModel.task_id
            )
        return stmt

    async with get_session() as session:
        pending_total = int(
            await session.scalar(
                _apply_join(select(func.count()).select_from(TaskVersionModel)).where(
                    and_(*filters)
                )
            )
            or 0
        )

        query = (
            _apply_join(select(TaskVersionModel))
            .where(and_(*filters))
            .order_by(TaskVersionModel.created_at.asc())
            .limit(limit)
        )
        rows = (await session.execute(query)).scalars().all()

        enqueued = 0
        for version_row in rows:
            row_org_id = None
            if version_row.task is not None:
                row_org_id = version_row.task.org_id
            await enqueue_task_expand_worker_job(
                session,
                task_id=version_row.task_id,
                version=version_row.version,
                org_id=row_org_id,
            )
            enqueued += 1

        await session.commit()

    return ExpandBackfillResponse(
        enqueued=enqueued,
        pending_total=pending_total,
    )
