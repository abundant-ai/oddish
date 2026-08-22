"""Authenticated endpoint for append-only QA review votes."""

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.feedback import create_feedback_core
from oddish.db import get_session
from oddish.schemas import FeedbackCreate, FeedbackResponse

router = APIRouter()


@router.post(
    "/experiments/{experiment_id}/feedback", response_model=FeedbackResponse
)
async def create_feedback(
    experiment_id: str,
    data: FeedbackCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> FeedbackResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        row = await create_feedback_core(
            session,
            data=data,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
        )
        await session.commit()
        return FeedbackResponse.model_validate(row)
