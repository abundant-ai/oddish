"""Authenticated QA prompt replay endpoint."""

from typing import Annotated

from auth import APIKeyScope, AuthContext, require_auth
from fastapi import APIRouter, Depends, Header
from idempotency_store import SubmissionIdempotencyStore
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.endpoints.qa_eval import (
    create_qa_eval_core,
    get_qa_eval_experiment_core,
)
from oddish.core.idempotency import IdempotencyReplay, compute_request_hash
from oddish.db import get_read_session, get_session
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
    QAEvalExperimentResponse,
)

router = APIRouter(tags=["QA evaluations"])


@router.get("/qa-evals/{experiment_id}", response_model=QAEvalExperimentResponse)
async def get_qa_eval_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QAEvalExperimentResponse:
    """Read one organization-scoped QA replay for the signed-in dashboard."""
    auth.require_scope(APIKeyScope.READ)
    async with get_read_session() as session:
        return await get_qa_eval_experiment_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
        )


@router.post("/qa-evals", response_model=QAEvalCreateResponse)
async def create_qa_eval(
    payload: QAEvalCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> QAEvalCreateResponse:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    async with get_session() as session:
        try:
            result = await create_qa_eval_core(
                session,
                request=payload,
                org_id=auth.org_id,
                owner_user_id=auth.user_id,
                billed_user_id=auth.user_id,
                idempotency_key=idempotency_key,
                idempotency_store=SubmissionIdempotencyStore(session),
                request_hash=compute_request_hash(payload),
            )
        except IdempotencyReplay as replay:
            return QAEvalCreateResponse.model_validate(replay.response_json)
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)
    return result
