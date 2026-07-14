"""REST endpoints for agent-eval analyzers.

Thin wrapper over ``oddish.core.analyzers``: authenticate, open a session,
delegate to the core function, commit, serialize. ``experiment_ids`` isn't
an ORM attribute on ``AnalyzerModel`` (it lives in the ``analyzer_experiments``
join table), so every response goes through ``_to_response`` to populate it.
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
from oddish.schemas import ExperimentOption, AnalyzerCreate, AnalyzerResponse

router = APIRouter(tags=["Analyzers"])


async def _to_response(session, analyzer) -> AnalyzerResponse:
    resp = AnalyzerResponse.model_validate(analyzer)
    resp.experiment_ids = await experiment_ids_for_analyzer(session, analyzer.id)
    return resp


@router.get("/analyzers/experiment-options", response_model=list[ExperimentOption])
async def experiment_options(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ExperimentOption]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_experiment_options_core(session, org_id=auth.org_id)


@router.post("/analyzers", response_model=AnalyzerResponse)
async def create_analyzer(
    data: AnalyzerCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AnalyzerResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        analyzer = await create_analyzer_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
        await session.refresh(analyzer)
        return await _to_response(session, analyzer)


@router.get("/analyzers", response_model=list[AnalyzerResponse])
async def list_analyzers(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[AnalyzerResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        analyzers = await list_analyzers_core(session, org_id=auth.org_id)
        return [await _to_response(session, r) for r in analyzers]


@router.get("/analyzers/{analyzer_id}", response_model=AnalyzerResponse)
async def get_analyzer(
    analyzer_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AnalyzerResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        analyzer = await get_analyzer_core(session, analyzer_id, org_id=auth.org_id)
        return await _to_response(session, analyzer)


@router.delete("/analyzers/{analyzer_id}")
async def delete_analyzer(
    analyzer_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        await delete_analyzer_core(session, analyzer_id, org_id=auth.org_id)
        await session.commit()
    return {"ok": True}
