"""Offline harness core for the trajectory-summary path.

Pure logic -- scope resolution, cohort selection, per-trial record building --
with no Modal imports, so it is importable from tests. ``ops_summary_dump.py``
is the thin Modal wrapper that supplies production credentials.

Enters production at ``summarize_trajectory.generate()``'s construction site
(``build_summary_block``), NOT at ``get_or_generate_summary``: the latter
returns a cached block whose ``schema_version`` matches, which would hand back
a stale summary instead of exercising a revised taxonomy.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from oddish.core.helpers import _has_fetchable_trajectory
from oddish.db.models import TaskModel, TrialModel

MAX_CONCURRENCY = 4


def validate_scope(
    *, trials: list[str] | None, task: str | None, experiment: str | None
) -> None:
    """Require exactly one scope. Raises before any query runs."""
    supplied = [name for name, value in
                (("--trials", trials), ("--task", task), ("--experiment", experiment))
                if value]
    if len(supplied) != 1:
        raise ValueError(
            f"Supply exactly one of --trials/--task/--experiment (got: {supplied or 'none'})"
        )


def filter_fetchable(rows, limit: int = 0) -> list:
    """Keep trials whose trajectory can actually be fetched, then apply limit.

    ``_has_fetchable_trajectory`` is a Python predicate, not a SQL condition --
    it also admits finished Grok Build trials that synthesize ATIF from
    grok-build.json. So the limit must be applied after filtering, or a cohort
    of N returns fewer than N.
    """
    kept = [t for t in rows if _has_fetchable_trajectory(t)]
    return kept[:limit] if limit else kept


async def resolve_cohort(
    session,
    *,
    trials: list[str] | None = None,
    task: str | None = None,
    experiment: str | None = None,
    limit: int = 0,
) -> list[TrialModel]:
    """Resolve a scope to an ordered, deterministic list of trials.

    Explicit ids are returned in the order given, unfiltered -- naming a probe
    trial is an intentional act. Task/experiment scopes exclude probes, order
    by trial id, and are then narrowed by ``filter_fetchable``.
    """
    validate_scope(trials=trials, task=task, experiment=experiment)
    stmt = select(TrialModel).options(selectinload(TrialModel.task))

    if trials:
        rows = (await session.execute(stmt.where(TrialModel.id.in_(trials)))).scalars().all()
        by_id = {t.id: t for t in rows}
        return [by_id[tid] for tid in trials if tid in by_id]

    if task:
        # Task names are the human-facing handle (unique per org); tasks.id is a
        # random primary key, so resolve through the tasks table.
        stmt = stmt.join(TaskModel, TaskModel.id == TrialModel.task_id).where(
            TaskModel.name == task
        )
    else:
        stmt = stmt.where(TrialModel.experiment_id == experiment)

    stmt = stmt.where(TrialModel.is_probe.is_(False)).order_by(TrialModel.id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return filter_fetchable(rows, limit=limit)
