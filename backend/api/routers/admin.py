"""Admin endpoints — auth wrapper over oddish core diagnostics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from auth import AuthContext, require_admin
from models import OrganizationModel, UserModel
from oddish.core.admin import (
    CostBreakdownResponse,
    QueueHealthResponse,
    QueueSlotsResponse,
    QueueStatusResponse,
    OrphanedStateResponse,
    WorkerJobsResponse,
    get_cost_breakdown_core,
    get_queue_health_core,
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


@router.get("/queue-health", response_model=QueueHealthResponse)
async def get_queue_health(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueHealthResponse:
    """Operator overview: throughput, per-queue-key capacity fill, and the
    persisted dispatcher/reconciler heartbeats.

    Answers "is the queue keeping up?" at a glance -- the panel that lets an
    operator self-diagnose "queued but not running" without psql + Modal logs.
    """
    async with get_session() as session:
        return await get_queue_health_core(session)


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


async def _enrich_cost_breakdown(session, result: CostBreakdownResponse) -> None:
    """Fill owner/org display names on a cost breakdown in place.

    The core aggregation in ``oddish`` only knows ``owner_user_id`` /
    ``org_id`` (the cloud auth tables it can't import). We resolve those to
    names here. ``include_deleted=True`` keeps historical spend attributable
    to users/orgs that have since been deactivated.
    """
    user_ids: set[str] = set()
    org_ids: set[str] = set()
    for entry in result.by_user:
        if entry.owner_user_id:
            user_ids.add(entry.owner_user_id)
        if entry.org_id:
            org_ids.add(entry.org_id)
    for experiment in result.experiments:
        if experiment.owner_user_id:
            user_ids.add(experiment.owner_user_id)
        if experiment.org_id:
            org_ids.add(experiment.org_id)
    # The by-user chart series keys are owner user ids too (plus the synthetic
    # "__other__" / "__unattributed__" keys, which carry their own labels).
    for series_key in result.series_by_user.keys:
        if series_key.key == series_key.label:  # unresolved -> a raw user id
            user_ids.add(series_key.key)

    users: dict[str, UserModel] = {}
    if user_ids:
        rows = await session.execute(
            select(UserModel)
            .where(UserModel.id.in_(user_ids))
            .execution_options(include_deleted=True)
        )
        for user in rows.scalars():
            users[user.id] = user
            if user.org_id:
                org_ids.add(user.org_id)

    orgs: dict[str, str] = {}
    if org_ids:
        rows = await session.execute(
            select(OrganizationModel.id, OrganizationModel.name)
            .where(OrganizationModel.id.in_(org_ids))
            .execution_options(include_deleted=True)
        )
        for org_id, org_name in rows.all():
            orgs[org_id] = org_name

    for entry in result.by_user:
        user = users.get(entry.owner_user_id) if entry.owner_user_id else None
        if user is not None:
            entry.name = user.name
            entry.email = user.email
            if not entry.org_id and user.org_id:
                entry.org_id = user.org_id
        entry.org_name = orgs.get(entry.org_id) if entry.org_id else None

    for experiment in result.experiments:
        owner_id = experiment.owner_user_id
        user = users.get(owner_id) if owner_id else None
        if user is not None:
            experiment.owner_name = user.name
            experiment.owner_email = user.email
        experiment.org_name = orgs.get(experiment.org_id) if experiment.org_id else None

    # Relabel by-user series keys (raw user ids) with display names.
    for series_key in result.series_by_user.keys:
        user = users.get(series_key.key)
        if user is not None:
            series_key.label = user.name or user.email or series_key.key


@router.get("/costs", response_model=CostBreakdownResponse)
async def get_costs(
    auth: Annotated[AuthContext, Depends(require_admin)],
    window_days: int = Query(
        7, ge=0, le=3650, description="Trailing window in days; 0 = all-time"
    ),
    experiment_limit: int = Query(100, ge=1, le=500),
    user_limit: int = Query(100, ge=1, le=500),
) -> CostBreakdownResponse:
    """Global trial-spend breakdown for the admin cost dashboard.

    Reports total cost over fixed trailing windows (24h / 7d / 30d / all-time)
    plus a per-user breakdown and a ranked table of the most expensive recent
    experiments with their model mix, for the selected ``window_days`` window.
    Cost uses the native runtime cost when reported and a per-model token
    estimate otherwise.
    """
    effective_window = None if window_days == 0 else window_days
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session,
            window_days=effective_window,
            experiment_limit=experiment_limit,
            user_limit=user_limit,
        )
        await _enrich_cost_breakdown(session, result)
    return result


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

    # Always join to ``tasks`` so the session-level soft-delete filter
    # excludes versions whose parent task has been tombstoned. The join
    # used to be gated on ``org_id`` being set, which silently leaked
    # soft-deleted tasks' versions into the backfill when no ``org_id``
    # was supplied.
    def _apply_join(stmt):
        return stmt.join(TaskModel, TaskModel.id == TaskVersionModel.task_id)

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
