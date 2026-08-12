"""Cross-task rollup of stored cohort comparisons. Read-only by construction."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import TaskModel, TaskVersionModel, TrialModel


@dataclass(frozen=True)
class RollupVersion:
    task_version_id: str
    task_id: str
    task_name: str
    version: int


async def resolve_rollup_versions(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> list[RollupVersion]:
    """Distinct task versions an experiment's trials belong to.

    Membership is ``trial_in_experiment``, the repo's one predicate for this:
    an ordinary experiment owns trials through ``trials.experiment_id`` while a
    collection gathers them through ``experiment_trials``, and either filter
    alone silently returns nothing for the other kind. The predicate also drops
    combine-copy duplicates, which would otherwise inflate a cohort and shift
    every baseline computed from it.
    """
    rows = (
        await session.execute(
            select(
                TrialModel.task_version_id,
                TaskModel.id,
                TaskModel.name,
                TaskVersionModel.version,
            )
            .join(TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id)
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(
                trial_in_experiment(experiment_id),
                TaskModel.org_id == org_id,
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.task_version_id.is_not(None),
            )
            .distinct()
        )
    ).all()
    return [
        RollupVersion(
            task_version_id=r[0], task_id=r[1], task_name=r[2], version=r[3]
        )
        for r in rows
    ]
