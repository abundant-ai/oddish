"""Authenticated QA prompt replay endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.endpoints.qa_eval import (
    create_qa_eval_core,
    get_qa_eval_results_core,
)
from oddish.db import get_session
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
    QAEvalResultsResponse,
)

router = APIRouter(tags=["QA evaluations"])


@router.post("/qa-evals", response_model=QAEvalCreateResponse)
async def create_qa_eval(
    payload: QAEvalCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QAEvalCreateResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        result = await create_qa_eval_core(
            session,
            request=payload,
            org_id=auth.org_id,
            owner_user_id=auth.user_id,
        )
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.get("/qa-evals/{experiment_ref}", response_model=QAEvalResultsResponse)
async def get_qa_eval_results(
    experiment_ref: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QAEvalResultsResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await get_qa_eval_results_core(
            session, experiment_ref=experiment_ref, org_id=auth.org_id
        )
