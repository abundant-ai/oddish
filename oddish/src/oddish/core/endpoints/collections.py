from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentModel, TrialModel, experiment_trials, utcnow
from oddish.db.models import TaskModel, TrialStatus
from oddish.schemas import TrialCollectionResponse

_TERMINAL = (TrialStatus.SUCCESS, TrialStatus.FAILED)


async def create_trial_collection_core(
    session: AsyncSession,
    *,
    name: str,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
    org_id: str | None,
) -> TrialCollectionResponse:
    """Gather existing trials into a new read-only collection experiment.

    Trials keep their home experiment; a fresh ``is_collection`` experiment is
    created and the trials are linked into it via ``experiment_trials`` /
    ``task_experiments`` (no copy). ``trial_ids`` links those exact trials;
    ``task_ids`` links each task's current-version terminal, non-superseded,
    non-probe trials. ``experiment_ids`` links each source experiment's
    terminal, non-superseded, non-probe trials, unioning join-table members
    when the source is itself a collection. The caller's session context
    manager commits.
    """
    from oddish.queue import _link_task_to_experiment

    explicit_ids = list(
        dict.fromkeys(t.strip() for t in (trial_ids or []) if t and t.strip())
    )
    task_idents = list(
        dict.fromkeys(t.strip() for t in (task_ids or []) if t and t.strip())
    )
    experiment_idents = list(
        dict.fromkeys(t.strip() for t in (experiment_ids or []) if t and t.strip())
    )
    if not explicit_ids and not task_idents and not experiment_idents:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one trial id, task id, or experiment id",
        )

    # 1. Explicit trials (existing behavior).
    explicit_rows: list[TrialModel] = []
    if explicit_ids:
        rows = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.id.in_(explicit_ids),
                    TrialModel.org_id == org_id,
                )
            )
        ).scalars().all()
        found = {t.id: t for t in rows}
        missing = [i for i in explicit_ids if i not in found]
        if missing:
            raise HTTPException(
                status_code=404, detail=f"Trials not found in org: {', '.join(missing)}"
            )
        explicit_rows = [found[i] for i in explicit_ids]

    # 2. Task-sourced trials (current version only).
    tasks_skipped_empty = 0
    task_rows: list[TrialModel] = []
    if task_idents:
        pairs: list[tuple[str, str]] = []
        for ident in task_idents:
            task = (
                await session.execute(
                    select(TaskModel).where(
                        or_(TaskModel.id == ident, TaskModel.name == ident),
                        TaskModel.org_id == org_id,
                    )
                )
            ).scalars().first()
            if task is None:
                raise HTTPException(status_code=404, detail=f"Task {ident} not found")
            if task.current_version_id is None:
                tasks_skipped_empty += 1
                continue
            pairs.append((task.id, task.current_version_id))

        if pairs:
            task_rows = (
                await session.execute(
                    select(TrialModel)
                    .where(
                        tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(pairs),
                        TrialModel.superseded_by_trial_id.is_(None),
                        TrialModel.status.in_(_TERMINAL),
                        TrialModel.is_probe.isnot(True),
                        TrialModel.org_id == org_id,
                    )
                    .order_by(TrialModel.task_id, TrialModel.created_at)
                )
            ).scalars().all()
            contributed = {t.task_id for t in task_rows}
            tasks_skipped_empty += sum(1 for tid, _ in pairs if tid not in contributed)

    # 2b. Experiment-sourced trials (link, never copy).
    experiments_skipped_empty = 0
    experiment_rows: list[TrialModel] = []
    if experiment_idents:
        exp_seen: set[str] = set()
        for ident in experiment_idents:
            source = (
                await session.execute(
                    select(ExperimentModel).where(
                        or_(ExperimentModel.id == ident, ExperimentModel.name == ident),
                        ExperimentModel.org_id == org_id,
                    )
                )
            ).scalars().first()
            if source is None:
                raise HTTPException(
                    status_code=404, detail=f"Experiment {ident} not found"
                )

            contributed = list(
                (
                    await session.execute(
                        select(TrialModel)
                        .where(
                            TrialModel.experiment_id == source.id,
                            TrialModel.superseded_by_trial_id.is_(None),
                            TrialModel.status.in_(_TERMINAL),
                            TrialModel.is_probe.isnot(True),
                            TrialModel.org_id == org_id,
                        )
                        .order_by(TrialModel.created_at)
                    )
                ).scalars().all()
            )
            # A collection owns no native trials; its members live in the join table.
            if source.is_collection:
                contributed.extend(
                    (
                        await session.execute(
                            select(TrialModel)
                            .join(
                                experiment_trials,
                                experiment_trials.c.trial_id == TrialModel.id,
                            )
                            .where(
                                experiment_trials.c.experiment_id == source.id,
                                experiment_trials.c.deleted_at.is_(None),
                                TrialModel.status.in_(_TERMINAL),
                                TrialModel.is_probe.isnot(True),
                                TrialModel.org_id == org_id,
                            )
                        )
                    ).scalars().all()
                )

            if not contributed:
                experiments_skipped_empty += 1
                continue
            for t in contributed:
                if t.id in exp_seen:
                    continue
                exp_seen.add(t.id)
                experiment_rows.append(t)

    # 3. Union + dedupe (explicit first).
    seen: set[str] = set()
    trials: list[TrialModel] = []
    for t in (*explicit_rows, *task_rows, *experiment_rows):
        if t.id in seen:
            continue
        seen.add(t.id)
        trials.append(t)
    if not trials:
        raise HTTPException(status_code=400, detail="resulting trial set is empty")

    explicit_id_set = {t.id for t in explicit_rows}
    task_id_set = {t.id for t in task_rows}
    trials_from_tasks = sum(
        1 for t in trials if t.id in task_id_set and t.id not in explicit_id_set
    )
    trials_from_experiments = sum(
        1
        for t in trials
        if t.id not in explicit_id_set and t.id not in task_id_set
    )

    # 4. Create the collection experiment and link additively.
    last_activity = max((t.created_at for t in trials), default=None) or utcnow()
    result = ExperimentModel(
        name=name.strip() or "collection",
        org_id=org_id,
        is_collection=True,
        last_activity_at=last_activity,
    )
    session.add(result)
    await session.flush()

    linked_task_ids = list(dict.fromkeys(t.task_id for t in trials))
    for task_id in linked_task_ids:
        await _link_task_to_experiment(session, task_id=task_id, experiment_id=result.id)

    await session.execute(
        insert(experiment_trials),
        [{"experiment_id": result.id, "trial_id": t.id} for t in trials],
    )

    return TrialCollectionResponse(
        id=result.id,
        name=result.name,
        trials_linked=len(trials),
        tasks_linked=len(linked_task_ids),
        trials_from_tasks=trials_from_tasks,
        tasks_skipped_empty=tasks_skipped_empty,
        trials_from_experiments=trials_from_experiments,
        experiments_skipped_empty=experiments_skipped_empty,
    )
