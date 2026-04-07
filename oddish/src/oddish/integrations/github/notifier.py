"""
GitHub PR notification service.

Handles updating PR comments when trial/analysis/verdict status changes.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import TaskModel, TrialModel, get_session

from .client import GitHubMeta, get_github_client
from .formatter import (
    TaskSummary,
    TrialSummary,
    format_experiment_comment,
    format_task_comment,
)

logger = logging.getLogger(__name__)

DASHBOARD_URL = os.getenv("ODDISH_DASHBOARD_URL", "https://www.oddish.app")


async def _build_trial_summary(
    trial: TrialModel, task_name: str | None = None
) -> TrialSummary:
    """Build a TrialSummary from a TrialModel."""
    duration_seconds = None
    if trial.started_at and trial.finished_at:
        duration_seconds = (trial.finished_at - trial.started_at).total_seconds()

    classification = None
    subtype = None
    if trial.analysis and isinstance(trial.analysis, dict):
        classification = trial.analysis.get("classification")
        subtype = trial.analysis.get("subtype")

    try:
        index = int(trial.id.split("-")[-1])
    except (ValueError, IndexError):
        index = 0

    return TrialSummary(
        index=index,
        trial_id=trial.id,
        agent=trial.agent,
        model=settings.normalize_trial_model(trial.agent, trial.model),
        status=trial.status.value if trial.status else "pending",
        reward=trial.reward,
        duration_seconds=duration_seconds,
        analysis_status=trial.analysis_status.value if trial.analysis_status else None,
        classification=classification,
        subtype=subtype,
        task_name=task_name,
    )


async def _build_task_summary(session: AsyncSession, task: TaskModel) -> TaskSummary:
    """Build a TaskSummary from a TaskModel."""
    result = await session.execute(
        select(TrialModel).where(TrialModel.task_id == task.id).order_by(TrialModel.id)
    )
    trials = result.scalars().all()

    trial_summaries = [
        await _build_trial_summary(t, task_name=task.name) for t in trials
    ]

    task_url = f"{DASHBOARD_URL}/experiments/{task.experiment_id}"

    return TaskSummary(
        task_id=task.id,
        task_name=task.name,
        task_url=task_url,
        trials=trial_summaries,
        verdict_status=task.verdict_status.value if task.verdict_status else None,
        verdict=task.verdict,
    )


async def _get_experiment_tasks(
    session: AsyncSession, experiment_id: str
) -> list[TaskModel]:
    """Get all tasks for an experiment (via task or trial link)."""
    has_trials_in_experiment = (
        select(TrialModel.task_id)
        .where(TrialModel.experiment_id == experiment_id)
        .distinct()
        .correlate(None)
        .scalar_subquery()
    )
    result = await session.execute(
        select(TaskModel)
        .where(
            or_(
                TaskModel.experiment_id == experiment_id,
                TaskModel.id.in_(has_trials_in_experiment),
            )
        )
        .order_by(TaskModel.created_at)
    )
    return list(result.scalars().all())


async def _update_pr_comment_for_task(task: TaskModel) -> bool:
    """Update the PR comment for a task. Returns True on success."""
    github_meta = GitHubMeta.from_tags(task.tags)
    if not github_meta:
        logger.debug(f"Task {task.id} has no GitHub metadata, skipping PR update")
        return False

    client = get_github_client()
    if not client.token:
        logger.warning("GITHUB_TOKEN not configured, skipping PR update")
        return False

    if not task.experiment_id:
        logger.debug(f"Task {task.id} has no experiment_id, skipping PR update")
        return False

    async with get_session() as session:
        task_summary = await _build_task_summary(session, task)

        experiment = task.experiment
        experiment_name = experiment.name if experiment else task.experiment_id
        experiment_url = f"{DASHBOARD_URL}/experiments/{task.experiment_id}"

        experiment_tasks = await _get_experiment_tasks(session, task.experiment_id)

        if len(experiment_tasks) > 1:
            task_summaries = [
                await _build_task_summary(session, t) for t in experiment_tasks
            ]
            comment_body = format_experiment_comment(
                tasks=task_summaries,
                experiment_name=experiment_name,
                experiment_url=experiment_url,
                dashboard_url=DASHBOARD_URL,
            )
        else:
            comment_body = format_task_comment(
                task=task_summary,
                experiment_name=experiment_name,
                experiment_url=experiment_url,
                dashboard_url=DASHBOARD_URL,
            )

    try:
        result = await client.upsert_oddish_comment(
            owner=github_meta.owner,
            repo=github_meta.repo,
            pr_number=github_meta.pr_number,
            body=comment_body,
        )
        if result:
            logger.info(
                f"Updated PR comment for {github_meta.owner}/{github_meta.repo}#{github_meta.pr_number}"
            )
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to update PR comment: {e}")
        return False


async def notify_trial_update(trial_id: str) -> bool:
    """Notify GitHub when a trial status changes."""
    try:
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if not trial:
                logger.warning(f"Trial {trial_id} not found for GitHub notification")
                return False

            task = await session.get(TaskModel, trial.task_id)
            if not task:
                logger.warning(
                    f"Task {trial.task_id} not found for GitHub notification"
                )
                return False

            return await _update_pr_comment_for_task(task)

    except Exception as e:
        logger.error(f"Error in notify_trial_update for {trial_id}: {e}")
        return False


async def notify_analysis_update(trial_id: str) -> bool:
    """Notify GitHub when an analysis completes."""
    try:
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if not trial:
                logger.warning(f"Trial {trial_id} not found for GitHub notification")
                return False

            task = await session.get(TaskModel, trial.task_id)
            if not task:
                logger.warning(
                    f"Task {trial.task_id} not found for GitHub notification"
                )
                return False

            return await _update_pr_comment_for_task(task)

    except Exception as e:
        logger.error(f"Error in notify_analysis_update for {trial_id}: {e}")
        return False


async def notify_verdict_update(task_id: str) -> bool:
    """Notify GitHub when a verdict completes."""
    try:
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            if not task:
                logger.warning(f"Task {task_id} not found for GitHub notification")
                return False

            return await _update_pr_comment_for_task(task)

    except Exception as e:
        logger.error(f"Error in notify_verdict_update for {task_id}: {e}")
        return False
