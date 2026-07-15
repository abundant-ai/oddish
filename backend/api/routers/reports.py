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

from fastapi import APIRouter, Depends, HTTPException

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.analyzers import (
    create_analyzer_core,
    delete_analyzer_core,
    experiment_ids_for_analyzer,
    get_analyzer_core,
    list_experiment_options_core,
    list_analyzers_core,
)
from oddish.db import JobStatus, get_session
from oddish.evals.analyzer.rollup import build_rollup
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


@router.get("/reports/{report_id}/rollup")
async def get_report_rollup(
    report_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        analyzer = await get_analyzer_core(session, report_id, org_id=auth.org_id)
        # A rollup is the product of a *successful* run, so status gates it --
        # findings alone can't. A retry resets a SUCCESS row to QUEUED without
        # clearing findings (jobs/handlers.py, deliberately, so the retry doesn't
        # no-op), which leaves the previous run's findings on an in-flight row;
        # keying off findings alone would serve that stale rollup as if current.
        if analyzer.status not in (JobStatus.SUCCESS, JobStatus.FAILED):
            raise HTTPException(
                status_code=409,
                detail=f"No current per-model data: this analyzer is {analyzer.status.value}.",
            )
        if analyzer.status is JobStatus.FAILED:
            # Any findings here are from an earlier successful run that a retry
            # has since superseded and failed -- not this row's result.
            raise HTTPException(
                status_code=404,
                detail=f"No per-model data: this analyzer failed. {analyzer.error or ''}".strip(),
            )
        if analyzer.findings is None:
            # SUCCESS with no findings is the one state that means legacy: the
            # worker writes findings and status = SUCCESS in the same commit.
            raise HTTPException(
                status_code=404,
                detail="No per-model data: this analyzer ran before findings were persisted.",
            )
        # NULL roster (ran before analyzers_006) passes through as None so the
        # 1a lane reports models_total: unknown. An empty dict is a real answer
        # -- "no trials" -- and must not collapse into the same value.
        return build_rollup(analyzer.findings, analyzer.models_by_task)


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
