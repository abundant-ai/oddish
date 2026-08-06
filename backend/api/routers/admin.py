"""Admin endpoints — auth wrapper over oddish core diagnostics."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, select
from sqlalchemy.exc import ProgrammingError

from auth import AuthContext, require_admin
from dashboard_attribution import resolve_github_users
from models import OrganizationModel, UserModel
from auth.permissions import is_operator_org, require_operator_org
from pg_errors import is_undefined_column_or_table_error
from slack_alert_settings import (
    AlertSettings,
    clear_alert_settings,
    get_alert_settings,
    set_alert_settings,
)
from oddish.core.admin import (
    CostBreakdownResponse,
    ModelConcurrencyAuditEntry,
    ModelConcurrencySetting,
    ModelConcurrencyUpdateRequest,
    QueueHealthResponse,
    QueueSlotsResponse,
    QueueStatusResponse,
    OrphanedStateResponse,
    UserCostBreakdownResponse,
    WorkerJobsResponse,
    get_cost_breakdown_core,
    get_model_concurrency_setting_core,
    get_queue_health_core,
    get_queue_slots_core,
    get_queue_status_core,
    get_orphaned_state_core,
    get_user_cost_breakdown_core,
    get_worker_jobs_admin_core,
    list_model_concurrency_audit_core,
    update_model_concurrency_core,
)
from oddish.core.trial_facets import rebuild_trial_facets_core
from oddish.db import TaskModel, TaskVersionModel, get_session
from oddish.queue import enqueue_task_expand_worker_job

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/slots", response_model=QueueSlotsResponse)
async def get_queue_slots(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueSlotsResponse:
    """Get current state of queue-key slot leases."""
    require_operator_org(auth)
    async with get_session() as session:
        return await get_queue_slots_core(session)


@router.get("/queue-status", response_model=QueueStatusResponse)
async def get_queue_status(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QueueStatusResponse:
    """Get queue status from the trials/tasks tables (the source of truth)."""
    async with get_session() as session:
        return await get_queue_status_core(
            session,
            org_id=None if is_operator_org(auth) else auth.org_id,
        )


@router.get("/orphaned-state", response_model=OrphanedStateResponse)
async def get_orphaned_state(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stale_after_minutes: int = Query(15, ge=1, le=240),
) -> OrphanedStateResponse:
    """Summarize stale queue/pipeline state."""
    async with get_session() as session:
        return await get_orphaned_state_core(
            session,
            stale_after_minutes=stale_after_minutes,
            org_id=auth.org_id,
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
    is_operator = is_operator_org(auth)
    async with get_session() as session:
        return await get_queue_health_core(
            session,
            org_id=None if is_operator else auth.org_id,
            include_global_details=is_operator,
        )


@router.put("/concurrency", response_model=ModelConcurrencySetting)
async def update_model_concurrency(
    request: ModelConcurrencyUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ModelConcurrencySetting:
    require_operator_org(auth)
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="Attributed user required")
    async with get_session() as session:
        return await update_model_concurrency_core(
            session,
            request,
            actor_user_id=auth.user_id,
            actor_api_key_id=auth.api_key_id,
        )


@router.get("/concurrency", response_model=ModelConcurrencySetting)
async def get_model_concurrency(
    queue_key: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ModelConcurrencySetting:
    require_operator_org(auth)
    if not queue_key.strip():
        raise HTTPException(status_code=422, detail="queue_key must not be blank")
    async with get_session() as session:
        return await get_model_concurrency_setting_core(session, queue_key)


@router.get("/concurrency/audit", response_model=list[ModelConcurrencyAuditEntry])
async def get_model_concurrency_audit(
    auth: Annotated[AuthContext, Depends(require_admin)],
    queue_key: str | None = None,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
) -> list[ModelConcurrencyAuditEntry]:
    require_operator_org(auth)
    if queue_key is not None and not queue_key.strip():
        raise HTTPException(status_code=422, detail="queue_key must not be blank")
    async with get_session() as session:
        return await list_model_concurrency_audit_core(
            session,
            queue_key=queue_key,
            before_id=before_id,
            limit=limit,
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
            org_id=auth.org_id,
        )


async def _enrich_cost_breakdown(
    session, result: CostBreakdownResponse, *, org_id: str
) -> None:
    """Fill in user and org display names on a cost breakdown."""
    user_ids: set[str] = set()
    for entry in result.by_user:
        # A None label means ``key`` is a real user id (the billed user, or the
        # submitting credential's user) whose name/email we resolve; a set label
        # is a self-describing fallback row (GitHub handle / Unattributed).
        if entry.label is None:
            user_ids.add(entry.key)
    for experiment in result.experiments:
        if experiment.owner_user_id:
            user_ids.add(experiment.owner_user_id)
    for series_key in result.series_by_user.keys:
        if series_key.key == series_key.label:
            user_ids.add(series_key.key)

    users: dict[str, UserModel] = {}
    if user_ids:
        rows = await session.execute(
            select(UserModel)
            .where(UserModel.id.in_(user_ids))
            .where(UserModel.org_id == org_id)
            .execution_options(include_deleted=True)
        )
        users = {user.id: user for user in rows.scalars()}

    org_name = await session.scalar(
        select(OrganizationModel.name)
        .where(OrganizationModel.id == org_id)
        .execution_options(include_deleted=True)
    )

    for entry in result.by_user:
        user = users.get(entry.key) if entry.label is None else None
        if user is not None:
            entry.name = user.name
            entry.email = user.email
            if not entry.org_id and user.org_id:
                entry.org_id = user.org_id
        entry.org_name = org_name if entry.org_id == org_id else None

    for experiment in result.experiments:
        owner_id = experiment.owner_user_id
        user = users.get(owner_id) if owner_id else None
        if user is not None:
            experiment.owner_name = user.name
            experiment.owner_email = user.email
        experiment.org_name = org_name if experiment.org_id == org_id else None

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
    """Return the active organization's billable-spend breakdown."""
    effective_window = None if window_days == 0 else window_days
    async with get_session() as session:
        result = await get_cost_breakdown_core(
            session,
            org_id=auth.org_id,
            window_days=effective_window,
            experiment_limit=experiment_limit,
            user_limit=user_limit,
            resolve_github_users=resolve_github_users,
        )
        await _enrich_cost_breakdown(session, result, org_id=auth.org_id)
    return result


@router.get("/costs/users/{user_id}", response_model=UserCostBreakdownResponse)
async def get_user_costs(
    auth: Annotated[AuthContext, Depends(require_admin)],
    user_id: str,
    window_days: int = Query(
        7, ge=0, le=3650, description="Trailing window in days; 0 = all-time"
    ),
    task_limit: int = Query(100, ge=1, le=500),
) -> UserCostBreakdownResponse:
    """Per-user billed spend over settled trials (finished_at axis, estimate-priced)."""
    effective_window = None if window_days == 0 else window_days
    async with get_session() as session:
        user = await session.get(
            UserModel, user_id, execution_options={"include_deleted": True}
        )
        if user is None or user.org_id != auth.org_id:
            raise HTTPException(status_code=404, detail="User not found")
        result = await get_user_cost_breakdown_core(
            session,
            org_id=auth.org_id,
            billed_user_id=user_id,
            window_days=effective_window,
            task_limit=task_limit,
        )
    result.name = user.name
    result.email = user.email
    result.github_username = user.github_username
    return result


class ExpandBackfillResponse(BaseModel):
    """Response for a single backfill batch.

    - ``enqueued`` is the number of ``TASK_EXPAND`` jobs scheduled by
      this call.
    - ``pending_total`` is the organization-scoped count of versions matching
      the current filters (``expanded_at IS NULL AND task_s3_key IS
      NOT NULL``, plus any ``task_id`` filter), measured
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
    limit: int = Query(500, ge=1, le=5000),
) -> ExpandBackfillResponse:
    """Enqueue ``TASK_EXPAND`` jobs for task versions that haven't been expanded.

    The handler is idempotent (keyed on the archive's etag via
    ``.oddish-manifest.json``) so callers can re-run the backfill
    without duplicating work.

    Only task versions belonging to the active organization are considered.
    """
    filters = [
        TaskVersionModel.expanded_at.is_(None),
        TaskVersionModel.task_s3_key.isnot(None),
        TaskModel.org_id == auth.org_id,
    ]
    if task_id:
        filters.append(TaskVersionModel.task_id == task_id)

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
            await enqueue_task_expand_worker_job(
                session,
                task_id=version_row.task_id,
                version=version_row.version,
                org_id=auth.org_id,
            )
            enqueued += 1

        await session.commit()

    return ExpandBackfillResponse(
        enqueued=enqueued,
        pending_total=pending_total,
    )


class SlackAlertSettingsResponse(BaseModel):
    trial_escalation_usd: float
    user_daily_overage_delta_usd: float
    always_ping_emails: list[str]
    # False means nobody has overridden anything and these are the values baked
    # into the deploy, which the UI labels rather than presenting as a choice.
    is_override: bool


class OperatorAccessResponse(BaseModel):
    allowed: bool


@router.get("/operator-access", response_model=OperatorAccessResponse)
async def get_operator_access(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> OperatorAccessResponse:
    return OperatorAccessResponse(allowed=is_operator_org(auth))


class TrialFacetsRefreshResponse(BaseModel):
    orgs: int
    rows: int


@router.post("/trial-facets/refresh", response_model=TrialFacetsRefreshResponse)
async def refresh_trial_facets(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> TrialFacetsRefreshResponse:
    """Rebuild the task-browser facet vocabulary on demand.

    The scheduler-neutral binding of ``oddish.core.trial_facets``: Modal
    deploys run the same rebuild on a Period schedule
    (``worker.refresh_trial_facets``); non-Modal deploys (``serve.py``)
    cron this endpoint instead. Also the manual ops lever after a bulk
    deletion.
    """
    async with get_session() as session:
        orgs, rows = await rebuild_trial_facets_core(session)
    return TrialFacetsRefreshResponse(orgs=orgs, rows=rows)


class SlackAlertSettingsRequest(BaseModel):
    trial_escalation_usd: float = Field(gt=0)
    user_daily_overage_delta_usd: float = Field(gt=0)
    always_ping_emails: list[str] = Field(max_length=50)

    @field_validator("always_ping_emails")
    @classmethod
    def _plausible_addresses(cls, emails: list[str]) -> list[str]:
        # Shape only -- Slack decides whether an address resolves to an account,
        # and a typo there costs one dropped mention rather than a bad write.
        # Full RFC validation would mean taking on email-validator for this.
        for email in emails:
            if not _EMAIL_RE.fullmatch(email.strip()):
                raise ValueError(f"not an email address: {email!r}")
        return [email.strip() for email in emails]


def _settings_response(settings: AlertSettings) -> SlackAlertSettingsResponse:
    return SlackAlertSettingsResponse(**asdict(settings))


def _unavailable(exc: ProgrammingError) -> HTTPException:
    if not is_undefined_column_or_table_error(exc):
        raise exc
    return HTTPException(
        status_code=503,
        detail=(
            "Slack alert settings are not available yet (schema is still "
            "migrating). Try again shortly."
        ),
    )


@router.get("/slack-alert-settings", response_model=SlackAlertSettingsResponse)
async def get_slack_alert_settings(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> SlackAlertSettingsResponse:
    """Effective Slack cost-alert thresholds and escalation list."""
    require_operator_org(auth)
    try:
        async with get_session() as session:
            return _settings_response(await get_alert_settings(session))
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc


@router.put("/slack-alert-settings", response_model=SlackAlertSettingsResponse)
async def update_slack_alert_settings(
    payload: SlackAlertSettingsRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> SlackAlertSettingsResponse:
    """Override the thresholds and escalation list for every org."""
    require_operator_org(auth)
    try:
        async with get_session() as session:
            settings = await set_alert_settings(
                session, **payload.model_dump(), updated_by_user_id=auth.user_id
            )
            return _settings_response(settings)
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc


@router.delete("/slack-alert-settings", response_model=SlackAlertSettingsResponse)
async def reset_slack_alert_settings(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> SlackAlertSettingsResponse:
    """Drop the override; the deploy-time defaults take over again."""
    require_operator_org(auth)
    try:
        async with get_session() as session:
            return _settings_response(await clear_alert_settings(session))
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc
