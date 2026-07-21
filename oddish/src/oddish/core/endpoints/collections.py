from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import (
    ExperimentModel,
    TrialModel,
    experiment_trials,
    task_experiments,
    utcnow,
)
from oddish.db.models import TaskModel, TrialStatus
from oddish.schemas import CollectionMutationResponse, TrialCollectionResponse

# Terminal statuses gathered into a collection. Includes SKIPPED so gate-skipped
# trials are preserved in the collection (like failed/errored trials), not
# dropped — they render as their own ⊘ state.
_TERMINAL = (TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED)


def _dedupe(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in (values or []) if v and v.strip()))


async def resolve_collection_sources(
    session: AsyncSession,
    *,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    from_experiment_ids: list[str] | None = None,
    org_id: str | None,
) -> tuple[list[TrialModel], int]:
    """Resolve collection sources to concrete trial rows, deduped.

    Shared by create and add so a ``--task name@N`` pin and a ``--from`` merge
    behave identically on both paths. Returns ``(rows, tasks_skipped_empty)``.
    An empty result is NOT an error here — callers raise their own message.
    """
    explicit_ids = _dedupe(trial_ids)
    task_idents = _dedupe(task_ids)
    source_experiment_ids = _dedupe(from_experiment_ids)

    # 1. Explicit trials: named individually, so no status filter.
    explicit_rows: list[TrialModel] = []
    if explicit_ids:
        rows = (
            (
                await session.execute(
                    select(TrialModel).where(
                        TrialModel.id.in_(explicit_ids),
                        TrialModel.org_id == org_id,
                    )
                )
            )
            .scalars()
            .all()
        )
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
                (
                    await session.execute(
                        select(TaskModel).where(
                            or_(TaskModel.id == ident, TaskModel.name == ident),
                            TaskModel.org_id == org_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if task is None:
                raise HTTPException(status_code=404, detail=f"Task {ident} not found")
            if task.current_version_id is None:
                tasks_skipped_empty += 1
                continue
            pairs.append((task.id, task.current_version_id))

        if pairs:
            task_rows = (
                (
                    await session.execute(
                        select(TrialModel)
                        .where(
                            tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(
                                pairs
                            ),
                            TrialModel.superseded_by_trial_id.is_(None),
                            TrialModel.status.in_(_TERMINAL),
                            TrialModel.is_probe.isnot(True),
                            TrialModel.org_id == org_id,
                        )
                        .order_by(TrialModel.task_id, TrialModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
            contributed = {t.task_id for t in task_rows}
            tasks_skipped_empty += sum(1 for tid, _ in pairs if tid not in contributed)

    # 3. Experiment-sourced trials. ``trial_in_experiment`` unions the source's
    # home trials with its gathered ones, so merging a collection in brings
    # across everything the source's own page shows -- a plain
    # ``TrialModel.experiment_id ==`` would silently drop the gathered half.
    experiment_rows: list[TrialModel] = []
    if source_experiment_ids:
        for exp_id in source_experiment_ids:
            exists = await session.scalar(
                select(ExperimentModel.id).where(
                    ExperimentModel.id == exp_id,
                    ExperimentModel.org_id == org_id,
                )
            )
            if exists is None:
                raise HTTPException(
                    status_code=404, detail=f"Experiment {exp_id} not found"
                )
        experiment_rows = (
            (
                await session.execute(
                    select(TrialModel)
                    .where(
                        or_(
                            *[trial_in_experiment(e) for e in source_experiment_ids]
                        ),
                        TrialModel.org_id == org_id,
                        TrialModel.superseded_by_trial_id.is_(None),
                        TrialModel.status.in_(_TERMINAL),
                        TrialModel.is_probe.isnot(True),
                    )
                    .order_by(TrialModel.task_id, TrialModel.created_at)
                )
            )
            .scalars()
            .all()
        )

    # 4. Union + dedupe (explicit first, so an explicitly named trial keeps its
    # position even if a bulk source would also have produced it).
    seen: set[str] = set()
    trials: list[TrialModel] = []
    for t in (*explicit_rows, *task_rows, *experiment_rows):
        if t.id in seen:
            continue
        seen.add(t.id)
        trials.append(t)
    return trials, tasks_skipped_empty


async def create_trial_collection_core(
    session: AsyncSession,
    *,
    name: str,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    from_experiment_ids: list[str] | None = None,
    org_id: str | None,
) -> TrialCollectionResponse:
    """Gather existing trials into a new read-only collection experiment.

    Trials keep their home experiment; a fresh ``is_collection`` experiment is
    created and the trials are linked into it via ``experiment_trials`` /
    ``task_experiments`` (no copy). ``trial_ids`` links those exact trials;
    ``task_ids`` links each task's current-version terminal, non-superseded,
    non-probe trials; ``from_experiment_ids`` links another experiment's
    trials. The caller's session context manager commits.
    """
    from oddish.queue import _link_task_to_experiment

    explicit_ids = _dedupe(trial_ids)
    if not explicit_ids and not _dedupe(task_ids) and not _dedupe(from_experiment_ids):
        raise HTTPException(
            status_code=400, detail="Provide at least one trial id or task id"
        )

    trials, tasks_skipped_empty = await resolve_collection_sources(
        session,
        trial_ids=trial_ids,
        task_ids=task_ids,
        from_experiment_ids=from_experiment_ids,
        org_id=org_id,
    )
    if not trials:
        raise HTTPException(status_code=400, detail="resulting trial set is empty")

    explicit_id_set = set(explicit_ids)
    trials_from_tasks = sum(1 for t in trials if t.id not in explicit_id_set)

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
        tasks_linked=len(linked_task_ids),
        trials_from_tasks=trials_from_tasks,
        tasks_skipped_empty=tasks_skipped_empty,
    )


async def _load_collection(
    session: AsyncSession, *, experiment_id: str, org_id: str | None
) -> ExperimentModel:
    """Fetch a collection experiment, or raise.

    404 (not 403) on a cross-org id so the route never confirms that another
    org's experiment exists. 409 on a real experiment: ``remove`` would
    silently no-op for trials owned via ``trials.experiment_id``, and mutating
    a running experiment's membership races the dispatcher.
    """
    experiment = (
        (
            await session.execute(
                select(ExperimentModel).where(
                    ExperimentModel.id == experiment_id,
                    ExperimentModel.org_id == org_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if experiment is None:
        raise HTTPException(
            status_code=404, detail=f"Experiment {experiment_id} not found"
        )
    if not experiment.is_collection:
        raise HTTPException(
            status_code=409, detail=f"experiment {experiment_id} is not a collection"
        )
    return experiment


async def _live_member_ids(session: AsyncSession, experiment_id: str) -> set[str]:
    """Trial ids with a living ``experiment_trials`` row.

    ``experiment_trials`` is a Core Table, so the soft-delete listener does not
    cover it -- the ``deleted_at`` filter has to be spelled out.
    """
    rows = (
        (
            await session.execute(
                select(experiment_trials.c.trial_id).where(
                    experiment_trials.c.experiment_id == experiment_id,
                    experiment_trials.c.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _live_member_task_ids(
    session: AsyncSession, experiment_id: str
) -> set[str]:
    """Tasks represented by a living ``experiment_trials`` row.

    ``include_deleted``: membership is the join row, not the trial's own
    ``deleted_at`` -- ``delete_trial_core`` soft-deletes a trial without
    tombstoning its membership, and the cost rollup still prices those rows
    (``experiment_cost.py`` opts into ``include_deleted`` too). Letting the
    listener filter them here would disagree with ``_live_member_ids`` and
    tear down a ``task_experiments`` link the rollup still charges for.
    """
    rows = (
        (
            await session.execute(
                select(TrialModel.task_id)
                .join(
                    experiment_trials,
                    experiment_trials.c.trial_id == TrialModel.id,
                )
                .where(
                    experiment_trials.c.experiment_id == experiment_id,
                    experiment_trials.c.deleted_at.is_(None),
                )
                .distinct()
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def add_to_collection_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    from_experiment_ids: list[str] | None = None,
    org_id: str | None,
) -> CollectionMutationResponse:
    """Link more trials into an existing collection. Append-only, idempotent."""
    from oddish.queue import _link_task_to_experiment, bump_experiment_last_activity

    experiment = await _load_collection(
        session, experiment_id=experiment_id, org_id=org_id
    )
    if not (
        _dedupe(trial_ids) or _dedupe(task_ids) or _dedupe(from_experiment_ids)
    ):
        raise HTTPException(status_code=400, detail="nothing to add")

    trials, _ = await resolve_collection_sources(
        session,
        trial_ids=trial_ids,
        task_ids=task_ids,
        from_experiment_ids=from_experiment_ids,
        org_id=org_id,
    )
    if not trials:
        raise HTTPException(status_code=400, detail="nothing to add")

    before_trials = await _live_member_ids(session, experiment_id)
    before_tasks = await _live_member_task_ids(session, experiment_id)

    # on_conflict_do_update rather than do_nothing: a trial removed earlier has
    # a tombstoned row, and do_nothing would leave it invisible forever. Mirrors
    # queue._link_task_to_experiment's restore-on-conflict.
    await session.execute(
        pg_insert(experiment_trials)
        .values([{"experiment_id": experiment_id, "trial_id": t.id} for t in trials])
        .on_conflict_do_update(
            index_elements=["experiment_id", "trial_id"],
            set_={"deleted_at": None},
        )
    )

    after_tasks = await _live_member_task_ids(session, experiment_id)
    newly_linked = after_tasks - before_tasks
    for task_id in sorted(newly_linked):
        await _link_task_to_experiment(
            session, task_id=task_id, experiment_id=experiment_id
        )

    await bump_experiment_last_activity(session, experiment_ids=experiment_id)

    added = {t.id for t in trials} - before_trials
    return CollectionMutationResponse(
        id=experiment.id,
        name=experiment.name,
        trials_added=len(added),
        trials_total=len(before_trials | {t.id for t in trials}),
        tasks_linked=len(newly_linked),
    )


async def _unlink_task_from_collection(
    session: AsyncSession, *, task_id: str, experiment_id: str
) -> None:
    """Tombstone a ``task_experiments`` link and invalidate its tag projection.

    Mirrors ``_link_task_to_experiment``'s restore path in reverse. Required,
    not cosmetic: the grid reaches gathered trials THROUGH the task row and the
    cost rollup counts live ``experiment_trials`` rows, so a stale link would
    price trials the page no longer shows (see endpoints/deletion.py).
    """
    from oddish.queue import _recompute_tag_projection_on_membership_removed

    await session.execute(
        update(task_experiments)
        .where(
            task_experiments.c.task_id == task_id,
            task_experiments.c.experiment_id == experiment_id,
            task_experiments.c.deleted_at.is_(None),
        )
        .values(deleted_at=utcnow())
    )
    org_id = await session.scalar(
        text("SELECT org_id FROM tasks WHERE id = :task_id"), {"task_id": task_id}
    )
    await _recompute_tag_projection_on_membership_removed(
        session, task_id=task_id, experiment_id=experiment_id, org_id=org_id
    )


async def remove_from_collection_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    org_id: str | None,
) -> CollectionMutationResponse:
    """Drop trials from a collection by tombstoning their membership rows.

    The trials themselves are untouched -- no artifact deletion, no change to
    ``trials.experiment_id``. Every reader filters ``deleted_at IS NULL``, so
    the dashboard and the public share link both update immediately.
    """
    from oddish.queue import bump_experiment_last_activity

    experiment = await _load_collection(
        session, experiment_id=experiment_id, org_id=org_id
    )
    explicit_ids = _dedupe(trial_ids)
    task_idents = _dedupe(task_ids)
    if not explicit_ids and not task_idents:
        raise HTTPException(status_code=400, detail="nothing to remove")

    before_trials = await _live_member_ids(session, experiment_id)
    before_tasks = await _live_member_task_ids(session, experiment_id)

    targets: set[str] = {i for i in explicit_ids if i in before_trials}

    # Unlike add, task removal is version-agnostic on purpose: removing a task
    # from a collection must take ALL of it, including trials from older
    # versions that were pinned in. Per-version removal goes through explicit
    # trial ids (the CLI's `--task name@N` expands to them client-side).
    for ident in task_idents:
        task = (
            (
                await session.execute(
                    select(TaskModel).where(
                        or_(TaskModel.id == ident, TaskModel.name == ident),
                        TaskModel.org_id == org_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task {ident} not found")
        # include_deleted: membership is the ``experiment_trials`` row, and a
        # trial soft-deleted by ``delete_trial_core`` keeps a live one. Without
        # this the listener hides those ids and the removal silently no-ops,
        # stranding the membership row and its task link. Same reason
        # ``deletion.py`` wraps its own trial-id subquery this way.
        task_trial_ids = (
            (
                await session.execute(
                    select(TrialModel.id)
                    .where(TrialModel.task_id == task.id)
                    .execution_options(include_deleted=True)
                )
            )
            .scalars()
            .all()
        )
        targets |= {i for i in task_trial_ids if i in before_trials}

    if not targets:
        return CollectionMutationResponse(
            id=experiment.id,
            name=experiment.name,
            trials_total=len(before_trials),
        )

    if not (before_trials - targets):
        raise HTTPException(
            status_code=409,
            detail="removing these trials would empty the collection",
        )

    # ``targets`` is already a set of literal ids, so this statement references
    # no mapped entity and the listener has nothing to attach to -- the
    # include_deleted opt-out belongs on the SELECTs above, which do.
    await session.execute(
        update(experiment_trials)
        .where(
            experiment_trials.c.experiment_id == experiment_id,
            experiment_trials.c.deleted_at.is_(None),
            experiment_trials.c.trial_id.in_(sorted(targets)),
        )
        .values(deleted_at=utcnow())
    )

    after_tasks = await _live_member_task_ids(session, experiment_id)
    orphaned = before_tasks - after_tasks
    for task_id in sorted(orphaned):
        await _unlink_task_from_collection(
            session, task_id=task_id, experiment_id=experiment_id
        )

    await bump_experiment_last_activity(session, experiment_ids=experiment_id)

    return CollectionMutationResponse(
        id=experiment.id,
        name=experiment.name,
        trials_removed=len(targets),
        trials_total=len(before_trials - targets),
        tasks_unlinked=len(orphaned),
    )


async def rename_collection_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    name: str,
    org_id: str | None,
) -> CollectionMutationResponse:
    """Rename a collection. The share token is untouched, so a published link
    keeps working under the new title."""
    from oddish.queue import bump_experiment_last_activity

    experiment = await _load_collection(
        session, experiment_id=experiment_id, org_id=org_id
    )
    stripped = (name or "").strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="name must not be empty")

    experiment.name = stripped
    await bump_experiment_last_activity(session, experiment_ids=experiment_id)

    return CollectionMutationResponse(
        id=experiment.id,
        name=stripped,
        trials_total=len(await _live_member_ids(session, experiment_id)),
    )
