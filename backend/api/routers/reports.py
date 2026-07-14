"""REST endpoints for agent-eval reports.

Thin wrapper over ``oddish.core.analyzers``: authenticate, open a session,
delegate to the core function, commit, serialize. The persistence layer is
still named "analyzer" (table ``analyzers`` / ``AnalyzerModel``); this HTTP
surface is the "report" seam. ``experiment_ids`` isn't an ORM attribute on
``AnalyzerModel`` (it lives in the ``analyzer_experiments`` join table), so
every response goes through ``_to_response`` to populate it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.analyzers import (
    create_analyzer_core,
    delete_analyzer_core,
    experiment_ids_for_analyzer,
    get_analyzer_core,
    list_experiment_options_core,
    list_analyzers_core,
)
from oddish.db import get_session
from oddish.schemas import ExperimentOption, ReportCreate, ReportResponse

router = APIRouter(tags=["Reports"])


async def _to_response(session, report) -> ReportResponse:
    resp = ReportResponse.model_validate(report)
    resp.experiment_ids = await experiment_ids_for_analyzer(session, report.id)
    return resp


@router.get("/reports/experiment-options", response_model=list[ExperimentOption])
async def experiment_options(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ExperimentOption]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_experiment_options_core(session, org_id=auth.org_id)


@router.post("/reports", response_model=ReportResponse)
async def create_report(
    data: ReportCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        report = await create_analyzer_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
        await session.refresh(report)
        return await _to_response(session, report)


@router.get("/reports", response_model=list[ReportResponse])
async def list_reports(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ReportResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        reports = await list_analyzers_core(session, org_id=auth.org_id)
        return [await _to_response(session, r) for r in reports]


@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ReportResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        report = await get_analyzer_core(session, report_id, org_id=auth.org_id)
        return await _to_response(session, report)


@router.delete("/reports/{report_id}")
async def delete_report(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await delete_analyzer_core(session, report_id, org_id=auth.org_id)
        await session.commit()
    return {"ok": True}
