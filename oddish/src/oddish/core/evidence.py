from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.helpers import build_trial_response
from oddish.db import (
    ExperimentCellModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)
from oddish.schemas import (
    EvidenceCellResponse,
    ExperimentCellAgentResponse,
    ExperimentCellResponse,
    ResolvedExperimentCellResponse,
    ResolvedExperimentResponse,
    TrialResponse,
)


ACTIVE_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
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

    rows = (
        (
            await session.execute(
                select(ExperimentCellModel, TaskVersionModel, TaskModel)
                .join(
                    TaskVersionModel,
                    TaskVersionModel.id == ExperimentCellModel.task_version_id,
                )
                .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
                .where(ExperimentCellModel.experiment_id == experiment_id)
                .order_by(
                    TaskModel.name.asc(),
                    TaskVersionModel.version.asc(),
                    ExperimentCellModel.provider.asc(),
                    ExperimentCellModel.model.asc(),
                    ExperimentCellModel.harness.asc(),
                )
            )
        )
        .all()
    )
    if not rows:
        return []

    responses: list[ResolvedExperimentCellResponse] = []
    for cell, version, task in rows:
        evidence_rows = (
            await session.execute(
                select(
                    func.count(TrialModel.id),
                    func.count(TrialModel.id).filter(
                        TrialModel.status == TrialStatus.SUCCESS
                    ),
                    func.count(TrialModel.id).filter(
                        TrialModel.status == TrialStatus.FAILED
                    ),
                    func.count(TrialModel.id).filter(
                        TrialModel.status.in_(ACTIVE_TRIAL_STATUSES)
                    ),
                    func.avg(TrialModel.reward),
                    func.max(TrialModel.finished_at),
                    func.array_agg(TrialModel.id),
                ).where(
                    TrialModel.task_version_id == cell.task_version_id,
                    TrialModel.agent_equivalence_key == cell.agent_equivalence_key,
                )
            )
        ).one()
        (
            have,
            have_successful,
            have_failed,
            have_running,
            mean_reward,
            last_run_at,
            trial_ids,
        ) = evidence_rows
        total_gap = max(0, cell.target_n_trials - int(have_successful or 0))
        responses.append(
            ResolvedExperimentCellResponse(
                cell=ExperimentCellResponse.model_validate(cell),
                id=cell.id,
                task_id=task.id,
                task_name=task.name,
                task_version=version.version,
                task_version_id=cell.task_version_id,
                target_n_trials=cell.target_n_trials,
                agent=ExperimentCellAgentResponse(
                    harness=cell.harness,
                    model=cell.model,
                    provider=cell.provider,
                    equivalence_key=cell.agent_equivalence_key,
                ),
                have_n_total=int(have or 0),
                have_n_successful=int(have_successful or 0),
                have_n_failed=int(have_failed or 0),
                have_n_running=int(have_running or 0),
                gap=total_gap,
                have_n_trials=int(have or 0),
                mean_reward=float(mean_reward) if mean_reward is not None else None,
                last_run_at=last_run_at,
                trial_ids=[str(trial_id) for trial_id in (trial_ids or [])],
            )
        )
    return responses


async def get_resolved_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> ResolvedExperimentResponse:
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id is not None:
        query = query.where(ExperimentModel.org_id == org_id)
    experiment = (await session.execute(query)).scalar_one_or_none()
    if experiment is None:
        raise HTTPException(
            status_code=404,
            detail=f"Experiment {experiment_id} not found",
        )

    cells = await get_experiment_cells_core(
        session,
        experiment_id=experiment_id,
        org_id=org_id,
    )
    return ResolvedExperimentResponse(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        cells=cells,
        total_gap=sum(cell.gap for cell in cells),
    )


async def list_experiment_cell_trials_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    org_id: str | None = None,
    limit: int = 500,
) -> list[TrialResponse]:
    query = (
        select(ExperimentCellModel, TaskModel.task_path)
        .join(
            TaskVersionModel,
            TaskVersionModel.id == ExperimentCellModel.task_version_id,
        )
        .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
        .where(
            ExperimentCellModel.id == cell_id,
            ExperimentCellModel.experiment_id == experiment_id,
        )
    )
    if org_id is not None:
        query = query.where(TaskModel.org_id == org_id)

    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Experiment cell {cell_id} not found",
        )

    cell, task_path = row
    trials = (
        (
            await session.execute(
                select(TrialModel)
                .where(
                    TrialModel.task_version_id == cell.task_version_id,
                    TrialModel.agent_equivalence_key == cell.agent_equivalence_key,
                )
                .order_by(
                    TrialModel.finished_at.desc().nullslast(),
                    TrialModel.created_at.desc(),
                    TrialModel.id.desc(),
                )
                .limit(max(1, min(limit, 2000)))
            )
        )
        .scalars()
        .all()
    )
    return [build_trial_response(trial, task_path) for trial in trials]
