"""Read + write paths for experiment cells.

An experiment is a saved selection of cells -- (task_version, agent,
target_n_trials) tuples. Resolving an experiment joins the cells against
the trial table on ``(task_version_id, agent_equivalence_key)`` to
compute "what evidence do we have / what's the gap".

Writes are all CRUD on ``experiment_cells``:
- create: append a new cell
- update: bump target_n_trials
- delete: remove a cell

Cells are frozen at save: we never mutate ``task_version_id`` on an
existing cell -- replacing one means delete + add.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.agent_identity import compute_agent_equivalence_key
from oddish.db import (
    ExperimentCellModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
)
from oddish.schemas import (
    ExperimentCellAgent,
    ExperimentCellCreateRequest,
    ExperimentCellResponse,
    ExperimentCellUpdateRequest,
    ResolvedExperimentCellResponse,
    ResolvedExperimentResponse,
)


_RUNNING_STATUSES = (
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
    TrialStatus.PENDING,
)


def _agent_from_cell(cell: ExperimentCellModel) -> ExperimentCellAgent:
    return ExperimentCellAgent(
        harness=cell.agent_harness,
        model=cell.agent_model,
        provider=cell.agent_provider,
        equivalence_key=cell.agent_equivalence_key,
    )


def _cell_to_response(cell: ExperimentCellModel) -> ExperimentCellResponse:
    return ExperimentCellResponse(
        id=cell.id,
        task_version_id=cell.task_version_id,
        target_n_trials=cell.target_n_trials,
        agent=_agent_from_cell(cell),
    )


async def _load_experiment(
    session: AsyncSession, *, experiment_id: str, org_id: str | None
) -> ExperimentModel:
    where = [ExperimentModel.id == experiment_id]
    if org_id is not None:
        where.append(ExperimentModel.org_id == org_id)
    exp = (
        await session.execute(select(ExperimentModel).where(*where))
    ).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp


async def list_cells_core(
    session: AsyncSession, *, experiment_id: str, org_id: str | None
) -> list[ExperimentCellResponse]:
    await _load_experiment(session, experiment_id=experiment_id, org_id=org_id)
    rows = (
        await session.execute(
            select(ExperimentCellModel)
            .where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.deleted_at.is_(None),
            )
            .order_by(
                ExperimentCellModel.task_version_id,
                ExperimentCellModel.agent_equivalence_key,
            )
        )
    ).scalars().all()
    return [_cell_to_response(c) for c in rows]


async def resolve_experiment_core(
    session: AsyncSession, *, experiment_id: str, org_id: str | None
) -> ResolvedExperimentResponse:
    """Compute the cell matrix + per-cell evidence counts."""
    exp = await _load_experiment(
        session, experiment_id=experiment_id, org_id=org_id
    )

    cells = (
        await session.execute(
            select(ExperimentCellModel)
            .where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.deleted_at.is_(None),
            )
            .order_by(
                ExperimentCellModel.task_version_id,
                ExperimentCellModel.agent_equivalence_key,
            )
        )
    ).scalars().all()

    if not cells:
        return ResolvedExperimentResponse(
            experiment_id=exp.id,
            experiment_name=exp.name,
            cells=[],
            total_gap=0,
        )

    # Aggregate trial evidence per (task_version, agent_equivalence_key).
    # Single grouped query; the composite index added in P1 makes this
    # cheap.
    pairs = list({(c.task_version_id, c.agent_equivalence_key) for c in cells})
    succ_expr = case((TrialModel.status == TrialStatus.SUCCESS, 1), else_=0)
    fail_expr = case((TrialModel.status == TrialStatus.FAILED, 1), else_=0)
    run_expr = case((TrialModel.status.in_(_RUNNING_STATUSES), 1), else_=0)

    # Postgres tuple-IN: SQLAlchemy's tuple_().in_() composes natively.
    from sqlalchemy import tuple_

    agg_rows = (
        await session.execute(
            select(
                TrialModel.task_version_id,
                TrialModel.agent_equivalence_key,
                func.count().label("total"),
                func.coalesce(func.sum(succ_expr), 0).label("succ"),
                func.coalesce(func.sum(fail_expr), 0).label("fail"),
                func.coalesce(func.sum(run_expr), 0).label("run"),
                func.avg(TrialModel.reward).label("mean_reward"),
                func.max(TrialModel.finished_at).label("last_run_at"),
            )
            .where(
                tuple_(
                    TrialModel.task_version_id,
                    TrialModel.agent_equivalence_key,
                ).in_(pairs)
            )
            .group_by(
                TrialModel.task_version_id, TrialModel.agent_equivalence_key
            )
        )
    ).all()
    aggs: dict[tuple[str, str], dict[str, Any]] = {
        (tv, ek): {
            "total": int(total),
            "succ": int(succ),
            "fail": int(fail),
            "run": int(run),
            "mean_reward": float(mean) if mean is not None else None,
            "last_run_at": last,
        }
        for tv, ek, total, succ, fail, run, mean, last in agg_rows
    }

    # Resolve task_version -> (task_id, task_name, version)
    tv_ids = list({c.task_version_id for c in cells})
    tv_rows = (
        await session.execute(
            select(
                TaskVersionModel.id,
                TaskVersionModel.task_id,
                TaskVersionModel.version,
                TaskModel.name,
            )
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(TaskVersionModel.id.in_(tv_ids))
        )
    ).all()
    tv_meta: dict[str, dict[str, Any]] = {
        tv_id: {"task_id": task_id, "version": version, "task_name": name}
        for tv_id, task_id, version, name in tv_rows
    }

    resolved: list[ResolvedExperimentCellResponse] = []
    total_gap = 0
    for c in cells:
        agg = aggs.get((c.task_version_id, c.agent_equivalence_key), {})
        succ = int(agg.get("succ", 0))
        gap = max(0, c.target_n_trials - succ)
        total_gap += gap
        meta = tv_meta.get(c.task_version_id, {})
        resolved.append(
            ResolvedExperimentCellResponse(
                id=c.id,
                task_version_id=c.task_version_id,
                task_id=meta.get("task_id", ""),
                task_name=meta.get("task_name"),
                task_version=meta.get("version"),
                target_n_trials=c.target_n_trials,
                agent=_agent_from_cell(c),
                have_n_total=int(agg.get("total", 0)),
                have_n_successful=succ,
                have_n_failed=int(agg.get("fail", 0)),
                have_n_running=int(agg.get("run", 0)),
                gap=gap,
                mean_reward=agg.get("mean_reward"),
                last_run_at=agg.get("last_run_at"),
            )
        )

    return ResolvedExperimentResponse(
        experiment_id=exp.id,
        experiment_name=exp.name,
        cells=resolved,
        total_gap=total_gap,
    )


async def create_experiment_core(
    session: AsyncSession,
    *,
    name: str,
    cells: list[ExperimentCellCreateRequest],
    org_id: str | None,
) -> ResolvedExperimentResponse:
    """Create a new experiment with optional initial cells.

    The experiment owns no trials and no tasks directly -- only its
    cells. Returns the resolved view so the caller can render the
    matrix immediately.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(
            status_code=400, detail="Experiment name cannot be empty"
        )
    exp = ExperimentModel(name=name, org_id=org_id)
    session.add(exp)
    await session.flush()

    for cell in cells:
        await add_cell_core(
            session,
            experiment_id=exp.id,
            payload=cell,
            org_id=org_id,
        )

    return await resolve_experiment_core(
        session, experiment_id=exp.id, org_id=org_id
    )


async def add_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    payload: ExperimentCellCreateRequest,
    org_id: str | None,
) -> ExperimentCellResponse:
    await _load_experiment(session, experiment_id=experiment_id, org_id=org_id)

    # Verify the task version exists (org-scoped via join on task).
    tv_check = (
        await session.execute(
            select(TaskVersionModel.id, TaskModel.org_id)
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(TaskVersionModel.id == payload.task_version_id)
        )
    ).first()
    if tv_check is None:
        raise HTTPException(status_code=404, detail="Task version not found")
    tv_id, tv_org = tv_check
    if org_id is not None and tv_org is not None and tv_org != org_id:
        raise HTTPException(status_code=404, detail="Task version not found")

    equivalence_key = compute_agent_equivalence_key(
        payload.agent_harness, payload.agent_model, payload.agent_provider
    )

    cell_id = generate_id()
    insert_stmt = (
        pg_insert(ExperimentCellModel.__table__)
        .values(
            id=cell_id,
            experiment_id=experiment_id,
            task_version_id=tv_id,
            agent_equivalence_key=equivalence_key,
            target_n_trials=payload.target_n_trials,
            agent_harness=payload.agent_harness,
            agent_model=payload.agent_model,
            agent_provider=payload.agent_provider,
        )
        .on_conflict_do_update(
            index_elements=[
                "experiment_id",
                "task_version_id",
                "agent_equivalence_key",
            ],
            set_={"target_n_trials": payload.target_n_trials},
        )
    )
    await session.execute(insert_stmt)
    await session.commit()

    cell = (
        await session.execute(
            select(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.task_version_id == tv_id,
                ExperimentCellModel.agent_equivalence_key == equivalence_key,
            )
        )
    ).scalar_one()
    return _cell_to_response(cell)


async def update_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    payload: ExperimentCellUpdateRequest,
    org_id: str | None,
) -> ExperimentCellResponse:
    await _load_experiment(session, experiment_id=experiment_id, org_id=org_id)
    cell = (
        await session.execute(
            select(ExperimentCellModel).where(
                ExperimentCellModel.id == cell_id,
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if cell is None:
        raise HTTPException(status_code=404, detail="Cell not found")
    cell.target_n_trials = payload.target_n_trials
    await session.commit()
    return _cell_to_response(cell)


async def delete_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    org_id: str | None,
) -> None:
    await _load_experiment(session, experiment_id=experiment_id, org_id=org_id)
    result = await session.execute(
        delete(ExperimentCellModel).where(
            ExperimentCellModel.id == cell_id,
            ExperimentCellModel.experiment_id == experiment_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Cell not found")
    await session.commit()
