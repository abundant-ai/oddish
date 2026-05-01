from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentCellModel, ExperimentModel, TaskVersionModel, TrialModel
from oddish.schemas import (
    EvidenceCellResponse,
    ExperimentCellResponse,
    ResolvedExperimentCellResponse,
)


async def get_task_version_evidence_core(
    session: AsyncSession,
    *,
    task_id: str,
    version: int,
    org_id: str | None = None,
) -> list[EvidenceCellResponse]:
    version_query = (
        select(TaskVersionModel)
        .where(TaskVersionModel.task_id == task_id, TaskVersionModel.version == version)
        .limit(1)
    )
    version_row = (await session.execute(version_query)).scalar_one_or_none()
    if version_row is None:
        return []

    if org_id is not None:
        org_check = await session.execute(
            text("SELECT 1 FROM tasks WHERE id = :task_id AND org_id = :org_id"),
            {"task_id": task_id, "org_id": org_id},
        )
        if org_check.first() is None:
            return []

    rows = (
        await session.execute(
            select(
                TrialModel.task_version_id,
                TrialModel.agent_equivalence_key,
                TrialModel.agent,
                TrialModel.model,
                TrialModel.provider,
                func.count(TrialModel.id),
                func.avg(TrialModel.reward),
                func.max(TrialModel.finished_at),
            )
            .where(
                TrialModel.task_version_id == version_row.id,
                TrialModel.agent_equivalence_key.isnot(None),
            )
            .group_by(
                TrialModel.task_version_id,
                TrialModel.agent_equivalence_key,
                TrialModel.agent,
                TrialModel.model,
                TrialModel.provider,
            )
        )
    ).all()

    return [
        EvidenceCellResponse(
            task_version_id=str(task_version_id),
            agent_equivalence_key=str(agent_equivalence_key),
            harness=str(agent),
            model=str(model or ""),
            provider=str(provider),
            n_trials=int(n_trials),
            mean_reward=float(mean_reward) if mean_reward is not None else None,
            last_run_at=last_run_at,
        )
        for (
            task_version_id,
            agent_equivalence_key,
            agent,
            model,
            provider,
            n_trials,
            mean_reward,
            last_run_at,
        ) in rows
    ]


async def get_experiment_cells_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> list[ResolvedExperimentCellResponse]:
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        query = query.where(ExperimentModel.org_id == org_id)
    experiment = (await session.execute(query)).scalar_one_or_none()
    if experiment is None:
        return []

    cells = (
        (
            await session.execute(
                select(ExperimentCellModel)
                .where(ExperimentCellModel.experiment_id == experiment_id)
                .order_by(
                    ExperimentCellModel.task_version_id.asc(),
                    ExperimentCellModel.provider.asc(),
                    ExperimentCellModel.model.asc(),
                    ExperimentCellModel.harness.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not cells:
        return []

    responses: list[ResolvedExperimentCellResponse] = []
    for cell in cells:
        evidence_rows = (
            await session.execute(
                select(
                    func.count(TrialModel.id),
                    func.avg(TrialModel.reward),
                    func.max(TrialModel.finished_at),
                    func.array_agg(TrialModel.id),
                ).where(
                    TrialModel.task_version_id == cell.task_version_id,
                    TrialModel.agent_equivalence_key == cell.agent_equivalence_key,
                )
            )
        ).one()
        have, mean_reward, last_run_at, trial_ids = evidence_rows
        responses.append(
            ResolvedExperimentCellResponse(
                cell=ExperimentCellResponse.model_validate(cell),
                have_n_trials=int(have or 0),
                mean_reward=float(mean_reward) if mean_reward is not None else None,
                last_run_at=last_run_at,
                trial_ids=[str(trial_id) for trial_id in (trial_ids or [])],
            )
        )
    return responses
