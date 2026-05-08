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
    ExperimentAgentModel,
    ExperimentCellModel,
    ExperimentModel,
    ExperimentTaskModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
)
from oddish.schemas import (
    CellAttempt,
    ExperimentCellAgent,
    ExperimentCellResponse,
    ExperimentCellUpdateRequest,
    ExperimentTaskRef,
    ResolvedExperimentCellResponse,
    ResolvedExperimentResponse,
)


MAX_ATTEMPTS_PER_CELL = 30


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


def _synthetic_cell_id(task_version_id: str, agent_eq_key: str) -> str:
    """Stable cross-product cell id used by the FE.

    Cells are now a derived cross product of the membership tables, so
    most cells don't have an underlying ``experiment_cells`` row. The
    FE still needs a stable handle for things like "load this cell's
    trials", so we synthesize one from the (task_version, agent) pair.
    """
    return f"{task_version_id}:{agent_eq_key}"


def _parse_synthetic_cell_id(cell_id: str) -> tuple[str, str] | None:
    if ":" not in cell_id:
        return None
    tv, _, eq = cell_id.partition(":")
    if not tv or not eq:
        return None
    return tv, eq


async def resolve_experiment_core(
    session: AsyncSession, *, experiment_id: str, org_id: str | None
) -> ResolvedExperimentResponse:
    """Compute the matrix as the cross product of the experiment's
    task and agent membership lists, with per-pair evidence counts.

    Per-pair ``target_n_trials`` overrides come from
    ``experiment_cells``; pairs without an override use the
    experiment's default ``target_n_trials``.
    """
    exp = await _load_experiment(
        session, experiment_id=experiment_id, org_id=org_id
    )

    task_rows = (
        await session.execute(
            select(ExperimentTaskModel)
            .where(ExperimentTaskModel.experiment_id == experiment_id)
            .order_by(ExperimentTaskModel.added_at)
        )
    ).scalars().all()
    agent_rows = (
        await session.execute(
            select(ExperimentAgentModel)
            .where(ExperimentAgentModel.experiment_id == experiment_id)
            .order_by(ExperimentAgentModel.added_at)
        )
    ).scalars().all()

    # Per-pair target override map.
    overrides_rows = (
        await session.execute(
            select(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    overrides: dict[tuple[str, str], ExperimentCellModel] = {
        (c.task_version_id, c.agent_equivalence_key): c
        for c in overrides_rows
    }

    # Aggregate evidence for the cross product. We query trials by the
    # full set of task_version_ids and agent_equivalence_keys, then
    # filter to the membership set in Python -- the index on (tv, eq)
    # makes the wider scan cheap and avoids tuple_().in_() blowup for
    # large matrices.
    tv_ids = [t.task_version_id for t in task_rows]
    eq_keys = [a.agent_equivalence_key for a in agent_rows]

    aggs: dict[tuple[str, str], dict[str, Any]] = {}
    if tv_ids and eq_keys:
        succ_expr = case((TrialModel.status == TrialStatus.SUCCESS, 1), else_=0)
        fail_expr = case((TrialModel.status == TrialStatus.FAILED, 1), else_=0)
        run_expr = case((TrialModel.status.in_(_RUNNING_STATUSES), 1), else_=0)
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
                    TrialModel.task_version_id.in_(tv_ids),
                    TrialModel.agent_equivalence_key.in_(eq_keys),
                )
                .group_by(
                    TrialModel.task_version_id,
                    TrialModel.agent_equivalence_key,
                )
            )
        ).all()
        aggs = {
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

    # Inline up to MAX_ATTEMPTS_PER_CELL trial pills per cell so the
    # matrix can render the sauron-style per-attempt squares without
    # an extra round trip. Beyond that cap the FE drops back to the
    # cell-trials drawer.
    attempts_by_pair: dict[tuple[str, str], list[CellAttempt]] = {}
    if tv_ids and eq_keys:
        attempt_rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.status,
                    TrialModel.reward,
                    TrialModel.task_version_id,
                    TrialModel.agent_equivalence_key,
                    TrialModel.finished_at,
                    TrialModel.created_at,
                )
                .where(
                    TrialModel.task_version_id.in_(tv_ids),
                    TrialModel.agent_equivalence_key.in_(eq_keys),
                )
                .order_by(
                    TrialModel.finished_at.asc().nullslast(),
                    TrialModel.created_at.asc(),
                )
            )
        ).all()
        for tid, status, reward, tv, ek, _fin, _cre in attempt_rows:
            key = (tv, ek)
            bucket = attempts_by_pair.setdefault(key, [])
            if len(bucket) >= MAX_ATTEMPTS_PER_CELL:
                continue
            bucket.append(
                CellAttempt(
                    id=tid,
                    status=status.value if hasattr(status, "value") else str(status),
                    reward=float(reward) if reward is not None else None,
                )
            )

    # Resolve task version metadata once.
    tv_meta: dict[str, dict[str, Any]] = {}
    if tv_ids:
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
        tv_meta = {
            tv_id: {"task_id": task_id, "version": version, "task_name": name}
            for tv_id, task_id, version, name in tv_rows
        }

    resolved: list[ResolvedExperimentCellResponse] = []
    total_gap = 0
    default_target = exp.target_n_trials
    for t in task_rows:
        meta = tv_meta.get(t.task_version_id, {})
        for a in agent_rows:
            override = overrides.get((t.task_version_id, a.agent_equivalence_key))
            target = override.target_n_trials if override else default_target
            agg = aggs.get((t.task_version_id, a.agent_equivalence_key), {})
            succ = int(agg.get("succ", 0))
            gap = max(0, target - succ)
            total_gap += gap
            cell_id = (
                override.id
                if override
                else _synthetic_cell_id(
                    t.task_version_id, a.agent_equivalence_key
                )
            )
            resolved.append(
                ResolvedExperimentCellResponse(
                    id=cell_id,
                    task_version_id=t.task_version_id,
                    task_id=meta.get("task_id", ""),
                    task_name=meta.get("task_name"),
                    task_version=meta.get("version"),
                    target_n_trials=target,
                    agent=ExperimentCellAgent(
                        harness=a.agent_harness,
                        model=a.agent_model,
                        provider=a.agent_provider,
                        equivalence_key=a.agent_equivalence_key,
                    ),
                    have_n_total=int(agg.get("total", 0)),
                    have_n_successful=succ,
                    have_n_failed=int(agg.get("fail", 0)),
                    have_n_running=int(agg.get("run", 0)),
                    gap=gap,
                    mean_reward=agg.get("mean_reward"),
                    last_run_at=agg.get("last_run_at"),
                    attempts=attempts_by_pair.get(
                        (t.task_version_id, a.agent_equivalence_key), []
                    ),
                )
            )

    tasks_payload = [
        ExperimentTaskRef(
            task_version_id=t.task_version_id,
            task_id=tv_meta.get(t.task_version_id, {}).get("task_id", ""),
            task_name=tv_meta.get(t.task_version_id, {}).get("task_name"),
            task_version=tv_meta.get(t.task_version_id, {}).get("version"),
        )
        for t in task_rows
    ]
    agents_payload = [
        ExperimentCellAgent(
            harness=a.agent_harness,
            model=a.agent_model,
            provider=a.agent_provider,
            equivalence_key=a.agent_equivalence_key,
        )
        for a in agent_rows
    ]

    return ResolvedExperimentResponse(
        experiment_id=exp.id,
        experiment_name=exp.name,
        target_n_trials=exp.target_n_trials,
        tasks=tasks_payload,
        agents=agents_payload,
        cells=resolved,
        total_gap=total_gap,
    )


async def create_experiment_core(
    session: AsyncSession,
    *,
    name: str,
    task_version_ids: list[str] | None = None,
    agents: list[Any] | None = None,
    target_n_trials: int | None = None,
    org_id: str | None,
) -> ResolvedExperimentResponse:
    """Create a new experiment seeded with task and agent membership.

    Tasks and agents are independent membership lists; the matrix is
    the cross product. Either side may be empty at creation.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(
            status_code=400, detail="Experiment name cannot be empty"
        )
    exp = ExperimentModel(name=name, org_id=org_id)
    if target_n_trials is not None and target_n_trials >= 1:
        exp.target_n_trials = target_n_trials
    session.add(exp)
    await session.flush()

    for tv_id in task_version_ids or []:
        await bulk_cells_core(
            session,
            experiment_id=exp.id,
            op="add_task",
            target_n_trials=exp.target_n_trials,
            agent_harness=None,
            agent_model=None,
            agent_provider=None,
            task_version_id=tv_id,
            org_id=org_id,
        )
    for a in agents or []:
        await bulk_cells_core(
            session,
            experiment_id=exp.id,
            op="add_agent",
            target_n_trials=exp.target_n_trials,
            agent_harness=getattr(a, "harness", None),
            agent_model=getattr(a, "model", None),
            agent_provider=getattr(a, "provider", None),
            task_version_id=None,
            org_id=org_id,
        )

    return await resolve_experiment_core(
        session, experiment_id=exp.id, org_id=org_id
    )


async def bulk_cells_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    op: str,
    target_n_trials: int,
    agent_harness: str | None,
    agent_model: str | None,
    agent_provider: str | None,
    task_version_id: str | None,
    org_id: str | None,
) -> int:
    """Add or remove members of an experiment.

    Tasks and agents are independent membership lists; the matrix is
    the cross product. Ops:

    - ``add_agent`` / ``add_agent_to_all_tasks`` -- insert into
      ``experiment_agents``.
    - ``add_task`` / ``add_task_to_all_agents`` -- insert into
      ``experiment_tasks``.
    - ``delete_agent`` -- remove from ``experiment_agents`` and any
      per-pair overrides referencing it.
    - ``delete_task`` / ``delete_task_version`` -- same for tasks.
    - ``set_default_target`` / ``bump_all_targets`` -- set
      ``experiments.target_n_trials`` and clear all per-pair overrides
      so the new default applies everywhere.

    The two name pairs are aliases; the long names are kept for FE
    compatibility while we transition. Returns the number of rows
    touched.
    """
    await _load_experiment(session, experiment_id=experiment_id, org_id=org_id)

    if op in ("set_default_target", "bump_all_targets"):
        exp = (
            await session.execute(
                select(ExperimentModel).where(
                    ExperimentModel.id == experiment_id
                )
            )
        ).scalar_one_or_none()
        if exp is not None:
            exp.target_n_trials = target_n_trials
        # Clearing per-pair overrides means the new default takes
        # effect everywhere, which is what users expect from "set
        # trials per cell to N".
        cleared = await session.execute(
            delete(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
            )
        )
        await session.flush()
        return cleared.rowcount or 0

    if op in ("add_agent", "add_agent_to_all_tasks"):
        if not agent_harness or not agent_provider:
            raise HTTPException(
                status_code=400,
                detail="agent_harness and agent_provider required",
            )
        equivalence_key = compute_agent_equivalence_key(
            agent_harness, agent_model, agent_provider
        )
        stmt = (
            pg_insert(ExperimentAgentModel.__table__)
            .values(
                experiment_id=experiment_id,
                agent_equivalence_key=equivalence_key,
                agent_harness=agent_harness,
                agent_model=agent_model,
                agent_provider=agent_provider,
            )
            .on_conflict_do_nothing(
                index_elements=["experiment_id", "agent_equivalence_key"],
            )
        )
        await session.execute(stmt)
        await session.flush()
        return 1

    if op in ("add_task", "add_task_to_all_agents"):
        if not task_version_id:
            raise HTTPException(
                status_code=400, detail="task_version_id required"
            )
        tv_check = (
            await session.execute(
                select(TaskVersionModel.id, TaskModel.org_id)
                .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
                .where(TaskVersionModel.id == task_version_id)
            )
        ).first()
        if tv_check is None:
            raise HTTPException(status_code=404, detail="Task version not found")
        _, tv_org = tv_check
        if org_id is not None and tv_org is not None and tv_org != org_id:
            raise HTTPException(status_code=404, detail="Task version not found")
        stmt = (
            pg_insert(ExperimentTaskModel.__table__)
            .values(
                experiment_id=experiment_id,
                task_version_id=task_version_id,
            )
            .on_conflict_do_nothing(
                index_elements=["experiment_id", "task_version_id"],
            )
        )
        await session.execute(stmt)
        await session.flush()
        return 1

    if op == "delete_agent":
        if not agent_harness or not agent_provider:
            raise HTTPException(
                status_code=400,
                detail="agent_harness and agent_provider required",
            )
        equivalence_key = compute_agent_equivalence_key(
            agent_harness, agent_model, agent_provider
        )
        # Drop from membership and clean up any per-pair overrides.
        await session.execute(
            delete(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.agent_equivalence_key == equivalence_key,
            )
        )
        result = await session.execute(
            delete(ExperimentAgentModel).where(
                ExperimentAgentModel.experiment_id == experiment_id,
                ExperimentAgentModel.agent_equivalence_key == equivalence_key,
            )
        )
        await session.flush()
        return result.rowcount or 0

    if op in ("delete_task", "delete_task_version"):
        if not task_version_id:
            raise HTTPException(
                status_code=400, detail="task_version_id required"
            )
        await session.execute(
            delete(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.task_version_id == task_version_id,
            )
        )
        result = await session.execute(
            delete(ExperimentTaskModel).where(
                ExperimentTaskModel.experiment_id == experiment_id,
                ExperimentTaskModel.task_version_id == task_version_id,
            )
        )
        await session.flush()
        return result.rowcount or 0

    raise HTTPException(
        status_code=400, detail=f"Unknown bulk op: {op}"
    )


async def update_cell_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    payload: ExperimentCellUpdateRequest,
    org_id: str | None,
) -> ExperimentCellResponse:
    """Set the per-pair target trials override for a (task, agent) cell.

    Most cells in the matrix don't have a backing ``experiment_cells``
    row -- they fall back to ``experiments.target_n_trials``. The
    ``cell_id`` may be either a real row id or a synthetic
    ``"{task_version_id}:{agent_equivalence_key}"`` handle. We
    upsert into ``experiment_cells`` either way.
    """
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
    if cell is not None:
        cell.target_n_trials = payload.target_n_trials
        await session.flush()
        return _cell_to_response(cell)

    # Synthetic id path: derive (task_version, agent) and upsert the
    # override row. We need the agent identity to denormalize, which
    # we recover from experiment_agents.
    parsed = _parse_synthetic_cell_id(cell_id)
    if parsed is None:
        raise HTTPException(status_code=404, detail="Cell not found")
    tv_id, eq_key = parsed
    agent_row = (
        await session.execute(
            select(ExperimentAgentModel).where(
                ExperimentAgentModel.experiment_id == experiment_id,
                ExperimentAgentModel.agent_equivalence_key == eq_key,
            )
        )
    ).scalar_one_or_none()
    task_row = (
        await session.execute(
            select(ExperimentTaskModel).where(
                ExperimentTaskModel.experiment_id == experiment_id,
                ExperimentTaskModel.task_version_id == tv_id,
            )
        )
    ).scalar_one_or_none()
    if agent_row is None or task_row is None:
        raise HTTPException(status_code=404, detail="Cell not found")
    new_id = generate_id()
    await session.execute(
        pg_insert(ExperimentCellModel.__table__)
        .values(
            id=new_id,
            experiment_id=experiment_id,
            task_version_id=tv_id,
            agent_equivalence_key=eq_key,
            target_n_trials=payload.target_n_trials,
            agent_harness=agent_row.agent_harness,
            agent_model=agent_row.agent_model,
            agent_provider=agent_row.agent_provider,
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
    await session.flush()
    cell = (
        await session.execute(
            select(ExperimentCellModel).where(
                ExperimentCellModel.experiment_id == experiment_id,
                ExperimentCellModel.task_version_id == tv_id,
                ExperimentCellModel.agent_equivalence_key == eq_key,
            )
        )
    ).scalar_one()
    return _cell_to_response(cell)


async def list_known_agents_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Distinct ``(harness, model, provider)`` identities seen in trials.

    Used by the experiment-builder agent picker so users select from
    real agents that have actually run instead of typing strings. Each
    row carries the canonical ``equivalence_key`` plus a usage count
    (recent trial count) so we can surface popular agents first.
    """
    where = []
    if org_id is not None:
        where.append(TrialModel.org_id == org_id)

    rows = (
        await session.execute(
            select(
                TrialModel.agent.label("harness"),
                TrialModel.model.label("model"),
                TrialModel.provider.label("provider"),
                TrialModel.agent_equivalence_key.label("equivalence_key"),
                func.count(TrialModel.id).label("trial_count"),
                func.max(TrialModel.created_at).label("last_seen"),
            )
            .where(
                TrialModel.agent.is_not(None),
                TrialModel.provider.is_not(None),
                TrialModel.agent_equivalence_key.is_not(None),
                *where,
            )
            .group_by(
                TrialModel.agent,
                TrialModel.model,
                TrialModel.provider,
                TrialModel.agent_equivalence_key,
            )
            .order_by(
                func.count(TrialModel.id).desc(),
                func.max(TrialModel.created_at).desc(),
            )
            .limit(max(1, min(limit, 500)))
        )
    ).all()

    return [
        {
            "harness": r.harness,
            "model": r.model,
            "provider": r.provider,
            "equivalence_key": r.equivalence_key,
            "trial_count": int(r.trial_count or 0),
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]


async def list_experiments_core(
    session: AsyncSession,
    *,
    org_id: str | None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List experiments visible to the org. Used by the FE pickers."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where = []
    if org_id is not None:
        where.append(ExperimentModel.org_id == org_id)
    rows = (
        await session.execute(
            select(ExperimentModel)
            .where(*where)
            .order_by(ExperimentModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [
        {
            "id": e.id,
            "name": e.name,
            "is_public": e.is_public,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


async def list_cell_trials_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    cell_id: str,
    org_id: str | None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List the trials that match a cell's (task_version, agent) pair.

    Returns a compact dict-per-trial for the FE drawer; not a full
    TrialResponse. Ordered by ``finished_at`` descending so the most
    recent runs surface first.
    """
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

    rows = (
        await session.execute(
            select(
                TrialModel.id,
                TrialModel.status,
                TrialModel.reward,
                TrialModel.error_message,
                TrialModel.started_at,
                TrialModel.finished_at,
                TrialModel.created_at,
                TrialModel.task_id,
                TrialModel.agent,
                TrialModel.model,
                TrialModel.provider,
            )
            .where(
                TrialModel.task_version_id == cell.task_version_id,
                TrialModel.agent_equivalence_key == cell.agent_equivalence_key,
            )
            .order_by(
                TrialModel.finished_at.desc().nullslast(),
                TrialModel.created_at.desc(),
            )
            .limit(max(1, min(limit, 1000)))
        )
    ).all()

    return [
        {
            "id": r.id,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "reward": r.reward,
            "error_message": r.error_message,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "task_id": r.task_id,
            "agent": r.agent,
            "model": r.model,
            "provider": r.provider,
        }
        for r in rows
    ]


