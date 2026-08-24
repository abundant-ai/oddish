"""Persist append-only QA votes after validating experiment membership."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import ExperimentModel, FeedbackModel, TrialModel
from oddish.schemas import FeedbackCreate


async def create_feedback_core(
    session: AsyncSession,
    *,
    data: FeedbackCreate,
    experiment_id: str,
    org_id: str | None,
    user_id: str | None,
) -> FeedbackModel:
    """Persist one QA vote; the caller owns the transaction."""
    trial_id = (
        await session.execute(
            select(TrialModel.id).where(
                TrialModel.id == data.trial_id,
                TrialModel.org_id == org_id,
                trial_in_experiment(experiment_id),
                select(ExperimentModel.id)
                .where(
                    ExperimentModel.id == experiment_id,
                    ExperimentModel.org_id == org_id,
                )
                .exists(),
            )
        )
    ).scalar_one_or_none()
    if trial_id is None:
        raise HTTPException(
            status_code=404, detail="trial not found in this experiment"
        )

    feedback = FeedbackModel(
        org_id=org_id,
        created_by_user_id=user_id,
        experiment_id=experiment_id,
        trial_id=trial_id,
        target=data.target,
        target_key=data.target_key,
        vote=data.vote,
        body=data.body,
    )
    session.add(feedback)
    await session.flush()
    return feedback
