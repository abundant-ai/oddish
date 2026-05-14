"""Authed CRUD + share + backfill endpoints for reports.

Public (anonymous) read endpoints live in
``backend/api/routers/reports_public.py`` to keep the surface clean.
"""

from __future__ import annotations

import logging
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import APIKeyScope, AuthContext, require_auth
from cloud_policy import (
    ALLOWED_CLOUD_ENVIRONMENTS,
    get_default_cloud_environment,
)
from models import APIKeyModel, UserModel
from oddish.core.reports import (
    build_report_list_item,
    build_report_response,
    build_share_response,
    compute_backfill_plan,
    ensure_report_public,
    execute_backfill,
    get_report_for_org,
    list_backfill_events,
    materialize_spec_into_report,
    resolve_report_cells,
    serialize_report_to_spec,
)
from oddish.db import (
    ReportModel,
    generate_id,
    get_session,
)
from oddish.schemas import (
    BackfillEventResponse,
    BackfillExecuteRequest,
    BackfillExecuteResponse,
    BackfillPlan,
    ReportListItem,
    ReportPatchRequest,
    ReportResponse,
    ReportShareResponse,
    ReportSpec,
)


router = APIRouter(tags=["Reports"])
logger = logging.getLogger(__name__)


async def _resolve_user_string(
    session: AsyncSession, auth: AuthContext
) -> str:
    """Mirror of tasks.py's actor resolution, trimmed to what backfill needs."""
    if auth.user is not None and auth.user.email:
        return auth.user.email
    if auth.user_id:
        user = await session.get(UserModel, auth.user_id)
        if user and user.email:
            return user.email
    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key:
            if api_key.created_by_user_id:
                user = await session.get(UserModel, api_key.created_by_user_id)
                if user and user.email:
                    return user.email
            if api_key.name:
                return api_key.name
    return "unknown"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("/reports", response_model=ReportResponse)
async def create_report(
    spec: ReportSpec,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportResponse:
    """Create a report from a validated spec.

    Unpacks the spec into the relational child tables in a single
    transaction. The response includes the resolved cell grid so the
    caller can immediately render it.
    """
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        report = ReportModel(
            id=generate_id(),
            name=spec.name,
            description=spec.description,
            org_id=auth.org_id,
            created_by_user_id=auth.user_id,
            spec_version=spec.version,
            trials_per_cell=spec.trials_per_cell,
            selection_strategy=spec.selection.strategy,
            selection_seed=spec.selection.seed,
            selection_tie_breaker=spec.selection.tie_breaker,
            source_include_superseded=spec.source.include_superseded,
            source_status=[str(s) for s in spec.source.status],
            backfill_enabled=spec.backfill.enabled,
            backfill_priority=spec.backfill.priority,
        )
        session.add(report)
        await session.flush()

        await materialize_spec_into_report(
            session, report, spec, org_id=auth.org_id
        )
        await session.commit()

        # Reload with eager-loaded relationships to render the response.
        report = await get_report_for_org(
            session, report_id=report.id, org_id=auth.org_id
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.get("/reports", response_model=list[ReportListItem])
async def list_reports(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ReportListItem]:
    """List reports for the authenticated org, newest first."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        result = await session.execute(
            select(ReportModel)
            .options(
                selectinload(ReportModel.agents),
                selectinload(ReportModel.task_versions),
            )
            .where(
                (ReportModel.org_id == auth.org_id)
                | (ReportModel.org_id.is_(None))
                if auth.org_id is None
                else ReportModel.org_id == auth.org_id
            )
            .order_by(ReportModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        reports = result.scalars().all()
        return [build_report_list_item(r) for r in reports]


# Order matters: ``/reports/{id}.yaml`` is declared BEFORE
# ``/reports/{id}`` so FastAPI's first-match wins picks the dotted
# variant; otherwise ``{report_id}`` swallows the ``.yaml`` suffix.
@router.get("/reports/{report_id}.yaml", response_class=Response)
async def get_report_yaml(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    """Return the canonical YAML rendering of a report's spec.

    Mirrors the ``oddish reports pull`` CLI verb. Authed only — public
    viewers cannot download the spec (see plan + sauron parity).
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        spec = serialize_report_to_spec(report)

    body = yaml.safe_dump(
        spec.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
    )
    return Response(
        content=body,
        media_type="application/yaml",
        headers={
            "Content-Disposition": f'attachment; filename="{report_id}.yaml"'
        },
    )


@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportResponse:
    """Fetch a single report including its resolved grid."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.patch("/reports/{report_id}", response_model=ReportResponse)
async def patch_report(
    report_id: str,
    payload: ReportPatchRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportResponse:
    """Patch a report's name, description, or spec (wholesale replace)."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )

        if payload.name is not None:
            report.name = payload.name.strip()
            if not report.name:
                raise HTTPException(
                    status_code=400, detail="Report name cannot be empty"
                )
        if payload.description is not None:
            report.description = payload.description or None

        if payload.spec is not None:
            await materialize_spec_into_report(
                session, report, payload.spec, org_id=auth.org_id
            )

        await session.commit()

        report = await get_report_for_org(
            session, report_id=report.id, org_id=auth.org_id
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.delete("/reports/{report_id}")
async def delete_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Soft-delete a report. The audit history and child rows stay in the DB."""
    auth.require_scope(APIKeyScope.TASKS)

    from datetime import datetime, timezone

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        report.deleted_at = datetime.now(timezone.utc)
        await session.commit()
        return {"status": "deleted", "report_id": report_id}


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


@router.get("/reports/{report_id}/share", response_model=ReportShareResponse)
async def get_report_share(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportShareResponse:
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        return build_share_response(report)


@router.post("/reports/{report_id}/share", response_model=ReportShareResponse)
async def publish_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportShareResponse:
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        await ensure_report_public(session, report)
        await session.commit()
        return build_share_response(report)


@router.delete("/reports/{report_id}/share", response_model=ReportShareResponse)
async def unpublish_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportShareResponse:
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        report.is_public = False
        await session.commit()
        return build_share_response(report)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@router.get(
    "/reports/{report_id}/backfill/plan", response_model=BackfillPlan
)
async def get_backfill_plan(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> BackfillPlan:
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        plan, _cells = await compute_backfill_plan(session, report)
        return plan


@router.post(
    "/reports/{report_id}/backfill/execute",
    response_model=BackfillExecuteResponse,
)
async def execute_report_backfill(
    report_id: str,
    payload: BackfillExecuteRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> BackfillExecuteResponse:
    """Submit a sweep to fill the missing cells.

    The ``confirm: true`` field on the body is the explicit user
    consent. The audit row is written *before* any sweep call so partial
    failures still leave a trace.
    """
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        user_string = await _resolve_user_string(session, auth)
        # The core helper commits the audit row before sweep work and
        # finalizes it on either success or failure, so the router does
        # not need to wrap this in its own try/commit.
        return await execute_backfill(
            session,
            report,
            cells=payload.cells,
            source=payload.source,
            initiated_by_user_id=auth.user_id,
            initiated_by_api_key_id=auth.api_key_id,
            user_string=user_string,
            org_id=auth.org_id,
            default_environment=get_default_cloud_environment(),
            allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
        )


@router.get(
    "/reports/{report_id}/backfill/events",
    response_model=list[BackfillEventResponse],
)
async def list_report_backfill_events(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[BackfillEventResponse]:
    """Backfill audit log for a report (most recent first)."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=auth.org_id
        )
        events = await list_backfill_events(
            session, report, limit=limit, offset=offset
        )

        # Hydrate ``initiated_by_display`` from UserModel/APIKeyModel — the
        # core helper stays display-agnostic so it doesn't bring backend
        # auth tables into oddish core.
        user_ids = {ev.initiated_by_user_id for ev in events if ev.initiated_by_user_id}
        api_key_ids = {
            ev.initiated_by_api_key_id
            for ev in events
            if ev.initiated_by_api_key_id
        }
        users: dict[str, UserModel] = {}
        if user_ids:
            user_rows = await session.execute(
                select(UserModel).where(UserModel.id.in_(user_ids))
            )
            users = {u.id: u for u in user_rows.scalars().all()}
        api_keys: dict[str, APIKeyModel] = {}
        if api_key_ids:
            key_rows = await session.execute(
                select(APIKeyModel).where(APIKeyModel.id.in_(api_key_ids))
            )
            api_keys = {k.id: k for k in key_rows.scalars().all()}

        for ev in events:
            if ev.initiated_by_user_id and ev.initiated_by_user_id in users:
                u = users[ev.initiated_by_user_id]
                ev.initiated_by_display = u.name or u.email
            elif ev.initiated_by_api_key_id and ev.initiated_by_api_key_id in api_keys:
                k = api_keys[ev.initiated_by_api_key_id]
                ev.initiated_by_display = f"API key: {k.name}"

        return events
