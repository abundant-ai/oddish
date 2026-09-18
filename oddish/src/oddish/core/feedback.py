"""Persist append-only QA votes after validating experiment or task membership."""

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


async def create_task_feedback_core(
    session: AsyncSession,
    *,
    data: FeedbackCreate,
    task_id: str,
    org_id: str | None,
    user_id: str | None,
) -> FeedbackModel:
    """Persist one vote cast from the task page; the caller owns the transaction.

    The trial must belong to the task. A pre-trial audit is itself a trial
    (``kind='audit'``, homed in a shadow experiment), so a vote on one of its
    findings is an action-item vote anchored to that trial.
    """
    experiment_id = (
        await session.execute(
            select(TrialModel.experiment_id).where(
                TrialModel.id == data.trial_id,
                TrialModel.task_id == task_id,
                TrialModel.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if experiment_id is None:
        raise HTTPException(status_code=404, detail="trial not found for this task")

    feedback = FeedbackModel(
        org_id=org_id,
        created_by_user_id=user_id,
        experiment_id=experiment_id,
        trial_id=data.trial_id,
        target=data.target,
        target_key=data.target_key,
        vote=data.vote,
        body=data.body,
    )
    session.add(feedback)
    await session.flush()
    return feedback
