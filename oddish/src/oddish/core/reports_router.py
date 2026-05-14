"""Auth-free reports router for the standalone OSS server.

The hosted backend (Clerk + multi-tenant) has its own wrapper in
``backend/api/routers/reports.py`` that adds org scoping and API-key
scope checks. This module owns the route shapes; the hosted wrapper
just re-defines them to attach ``require_auth`` and translate
``AuthContext.org_id`` into ``org_id=...`` kwargs.

Kept thin so the two layers don't drift — both delegate to the helpers
in ``oddish.core.reports``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

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


# OSS standalone has no org concept — everything is org_id=None.
_OSS_ORG_ID: str | None = None


@router.post("/reports", response_model=ReportResponse)
async def create_report(spec: ReportSpec) -> ReportResponse:
    async with get_session() as session:
        report = ReportModel(
            id=generate_id(),
            name=spec.name,
            description=spec.description,
            org_id=_OSS_ORG_ID,
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
            session, report, spec, org_id=_OSS_ORG_ID
        )
        await session.commit()

        report = await get_report_for_org(
            session, report_id=report.id, org_id=_OSS_ORG_ID
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.get("/reports", response_model=list[ReportListItem])
async def list_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ReportListItem]:
    async with get_session() as session:
        result = await session.execute(
            select(ReportModel)
            .options(
                selectinload(ReportModel.agents),
                selectinload(ReportModel.task_versions),
            )
            .order_by(ReportModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [build_report_list_item(r) for r in result.scalars().all()]


# Order matters: ``/reports/{id}.yaml`` must be declared BEFORE
# ``/reports/{id}`` so FastAPI's first-match wins picks up the dotted
# variant first; otherwise ``{report_id}`` swallows the ``.yaml`` suffix.
@router.get("/reports/{report_id}.yaml", response_class=Response)
async def get_report_yaml(report_id: str) -> Response:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        spec = serialize_report_to_spec(report)
    body = yaml.safe_dump(
        spec.model_dump(mode="json"), sort_keys=False, default_flow_style=False
    )
    return Response(
        content=body,
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{report_id}.yaml"'},
    )


@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report(report_id: str) -> ReportResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.patch("/reports/{report_id}", response_model=ReportResponse)
async def patch_report(
    report_id: str, payload: ReportPatchRequest
) -> ReportResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        if payload.name is not None:
            stripped = payload.name.strip()
            if not stripped:
                raise HTTPException(
                    status_code=400, detail="Report name cannot be empty"
                )
            report.name = stripped
        if payload.description is not None:
            report.description = payload.description or None
        if payload.spec is not None:
            await materialize_spec_into_report(
                session, report, payload.spec, org_id=_OSS_ORG_ID
            )
        await session.commit()

        report = await get_report_for_org(
            session, report_id=report.id, org_id=_OSS_ORG_ID
        )
        rows, columns, cells = await resolve_report_cells(session, report)
        return build_report_response(report, rows, columns, cells)


@router.delete("/reports/{report_id}")
async def delete_report(report_id: str) -> dict:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        report.deleted_at = datetime.now(timezone.utc)
        await session.commit()
        return {"status": "deleted", "report_id": report_id}


@router.get("/reports/{report_id}/share", response_model=ReportShareResponse)
async def get_report_share(report_id: str) -> ReportShareResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        return build_share_response(report)


@router.post("/reports/{report_id}/share", response_model=ReportShareResponse)
async def publish_report(report_id: str) -> ReportShareResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        await ensure_report_public(session, report)
        await session.commit()
        return build_share_response(report)


@router.delete("/reports/{report_id}/share", response_model=ReportShareResponse)
async def unpublish_report(report_id: str) -> ReportShareResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        report.is_public = False
        await session.commit()
        return build_share_response(report)


@router.get(
    "/reports/{report_id}/backfill/plan", response_model=BackfillPlan
)
async def get_backfill_plan(report_id: str) -> BackfillPlan:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        plan, _cells = await compute_backfill_plan(session, report)
        return plan


@router.post(
    "/reports/{report_id}/backfill/execute",
    response_model=BackfillExecuteResponse,
)
async def execute_report_backfill(
    report_id: str, payload: BackfillExecuteRequest
) -> BackfillExecuteResponse:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        return await execute_backfill(
            session,
            report,
            cells=payload.cells,
            source=payload.source,
            initiated_by_user_id=None,
            initiated_by_api_key_id=None,
            user_string="local",
            org_id=_OSS_ORG_ID,
        )


@router.get(
    "/reports/{report_id}/backfill/events",
    response_model=list[BackfillEventResponse],
)
async def list_report_backfill_events(
    report_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[BackfillEventResponse]:
    async with get_session() as session:
        report = await get_report_for_org(
            session, report_id=report_id, org_id=_OSS_ORG_ID
        )
        return await list_backfill_events(
            session, report, limit=limit, offset=offset
        )
