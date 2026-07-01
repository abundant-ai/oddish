from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentModel, TrialModel, experiment_trials, utcnow
from oddish.schemas import TrialCollectionResponse


async def create_trial_collection_core(
    session: AsyncSession,
    *,
    name: str,
    trial_ids: list[str],
    org_id: str | None,
) -> TrialCollectionResponse:
    """Gather existing trials into a new read-only collection experiment.

    The source trials (and their home experiments) are left untouched; a
    fresh ``is_collection`` experiment is created and the trials are
    additively linked into it via ``experiment_trials``/``task_experiments``,
    without copying or moving any data.

    The caller's session context manager is responsible for the commit.
    """
    from oddish.queue import _link_task_to_experiment

    ids = list(dict.fromkeys(t.strip() for t in trial_ids if t and t.strip()))
    if not ids:
        raise HTTPException(status_code=400, detail="Provide at least one trial id")

    rows = (
        await session.execute(
            select(TrialModel).where(
                TrialModel.id.in_(ids),
                TrialModel.org_id == org_id,
            )
        )
    ).scalars().all()

    found = {t.id: t for t in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Trials not found in org: {', '.join(missing)}",
        )

    trials = [found[i] for i in ids]
    last_activity = max((t.created_at for t in trials), default=None) or utcnow()

    result = ExperimentModel(
        name=name.strip() or "collection",
        org_id=org_id,
        is_collection=True,
        last_activity_at=last_activity,
    )
    session.add(result)
    await session.flush()

    task_ids = list(dict.fromkeys(t.task_id for t in trials))
    for task_id in task_ids:
        await _link_task_to_experiment(
            session, task_id=task_id, experiment_id=result.id
        )

    await session.execute(
        insert(experiment_trials),
        [{"experiment_id": result.id, "trial_id": t.id} for t in trials],
    )

    return TrialCollectionResponse(
        id=result.id,
        name=result.name,
        trials_linked=len(trials),
        tasks_linked=len(task_ids),
    )
