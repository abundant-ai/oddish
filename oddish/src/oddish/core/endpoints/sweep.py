from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Sequence

from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints._common import (
    get_task_for_org_core,
    _primary_experiment_for_task_model,
)
from oddish.core.harbor_source import (
    HarborSourceError,
    resolve_and_gate_harbor,
)
from oddish.core.idempotency import (
    SWEEP_ROUTE,
    IdempotencyConflict,
    IdempotencyStore,
    Reservation,
    compute_request_hash,
    reserve_idempotency_slot,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    utcnow,
)
from oddish.schemas import (
    TaskResponse,
    TaskSweepBatchItemResult,
    TaskSweepSubmission,
)


def build_task_sweep_response(
    task: TaskModel,
    new_trials: list[TrialModel],
    is_append: bool,
    experiment: ExperimentModel | None,
) -> TaskResponse:
    """Build the ``TaskResponse`` for a sweep submission.

    Shared by both ``POST /tasks/sweep`` routes and by the idempotency layer so
    the response stored for replay is identical to the one a fresh submission
    returns. For append submissions only the newly appended trials are counted;
    for create submissions the task's full trial set is counted.
    """
    response_trials = new_trials if is_append else list(task.trials)
    provider_counts: Counter[str] = Counter(trial.provider for trial in response_trials)
    primary = experiment or (task.experiments[0] if task.experiments else None)
    return TaskResponse(
        id=task.id,
        name=task.name,
        status=task.status,
        priority=task.priority,
        trials_count=len(response_trials),
        providers=dict(provider_counts),
        experiment_id=primary.id if primary else None,
        experiment_name=primary.name if primary else None,
        created_at=task.created_at,
        new_trial_ids=[trial.id for trial in response_trials],
    )


async def create_task_sweep_core(
    session: AsyncSession,
    *,
    submission: TaskSweepSubmission,
    org_id: str | None = None,
    default_environment: EnvironmentType | None = None,
    allowed_environments: Collection[EnvironmentType] | None = None,
    idempotency_key: str | None = None,
    idempotency_store: IdempotencyStore | None = None,
    request_hash: str | None = None,
) -> tuple[TaskModel, list[TrialModel], bool, ExperimentModel | None]:
    """
    Expands a sweep submission into trials and either appends to an existing task
    or creates a new one.

    Returns a tuple of (task, new_trials, is_append, experiment).

    When ``idempotency_key`` and ``idempotency_store`` are supplied (the cloud
    backend wires both; the open-source server passes neither), the submission is
    deduplicated: a faithful retry of a completed key raises ``IdempotencyReplay``
    carrying the stored response, and a key reused with a different body -- or one
    still in progress -- raises ``HTTPException(409)``. This short-circuits before
    any trials are created, so a retried "create" never duplicates trials via the
    auto-append flip below.

    ``request_hash`` is the fingerprint used to detect a key reused with a
    different body. Callers that mutate the submission before calling (the cloud
    backend resolves identity / attribution / probe defaults) must pass a hash
    of the *raw* client submission so an honest retry is not spuriously rejected;
    when omitted it is computed from ``submission`` as received here.
    """
    from oddish.core.sweeps import (
        build_trial_specs_from_sweep,
        build_task_submission_from_sweep,
    )
    from oddish.queue import (
        append_trials_to_task,
        create_task,
        get_experiment_by_id_or_name,
        get_or_create_experiment,
    )
    from oddish.core.tasks import resolve_task_storage
    from oddish.task_timeouts import TaskTimeoutValidationError
    from oddish.core.probe.auto_probe import maybe_enqueue_auto_probe

    # Reserve the idempotency slot before doing any work. The fingerprint comes
    # from the caller's raw pre-mutation snapshot when supplied (the backend
    # mutates the submission before calling), else from the submission as
    # received here -- in both cases captured before the link defaulting and
    # auto-append flip below mutate it, so the original create and a faithful
    # retry fingerprint identically. ``reserve_idempotency_slot`` raises
    # ``IdempotencyReplay`` on a matching retry (handled by the route) or
    # ``IdempotencyConflict`` (mapped to 409 here) on a reused key / in-progress
    # duplicate.
    reservation: Reservation | None = None
    if idempotency_store is not None and idempotency_key and org_id:
        effective_request_hash = (
            request_hash
            if request_hash is not None
            else compute_request_hash(submission)
        )
        try:
            reservation = await reserve_idempotency_slot(
                idempotency_store,
                org_id=org_id,
                route=SWEEP_ROUTE,
                raw_key=idempotency_key,
                request_hash=effective_request_hash,
                now=utcnow(),
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Resolve the Harbor pin to a concrete SHA, allowlist-check it, and stamp it
    # BEFORE any task mutation (append/create) so a disallowed/unresolvable ref
    # never half-creates a task. The default pin does no network I/O.
    from oddish.config import settings

    try:
        stamped_harbor, _variant = resolve_and_gate_harbor(
            submission.harbor, settings=settings
        )
    except HarborSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    submission = submission.model_copy(update={"harbor": stamped_harbor})

    # Default the task link to the GitHub PR URL when the caller didn't
    # pass an explicit ``--link`` but the task carries GitHub PR metadata
    # (set via ``--github-meta``). An explicit link always wins.
    if not submission.link:
        from oddish.integrations.github.client import GitHubMeta

        github_meta = GitHubMeta.from_tags(submission.tags)
        if github_meta and github_meta.pr_url:
            submission = submission.model_copy(update={"link": github_meta.pr_url})

    # Auto-detect append mode if the task already exists in the DB for this org.
    if not submission.append_to_task:
        existing = await session.get(TaskModel, submission.task_id)
        if existing is not None and (org_id is None or existing.org_id == org_id):
            submission = submission.model_copy(update={"append_to_task": True})

    if submission.append_to_task:
        task = await get_task_for_org_core(
            session, task_id=submission.task_id, org_id=org_id
        )
        await session.refresh(task, with_for_update=True)
        # Allow flipping task.run_analysis from False to True on append.
        # ``run_analysis`` runs at trial-completion time, so updating the
        # task-level flag does not retroactively analyze pre-existing
        # trials, but new trials submitted with ``--run-analysis`` will be
        # analyzed as the caller requested. This matches the documented
        # purpose of ``--force-new-version`` (see ``TaskUploadInitRequest``)
        # and lets a task that was first registered without analysis later
        # opt in without manual intervention.
        if submission.run_analysis and not task.run_analysis:
            task.run_analysis = True
        # Same opt-in flip for auto-probe: a task first run without probes can
        # later opt in on append. Off by default (probes are opt-in).
        if submission.run_probe and not task.run_probe:
            task.run_probe = True
        # Update the link whenever a new submission carries one (explicit
        # --link or derived from --github-meta above). A submission with no
        # link leaves the existing value untouched rather than clearing it.
        if submission.link:
            task.link = submission.link

        new_experiment_id: str | None = None
        experiment: ExperimentModel | None = None
        primary_experiment = await _primary_experiment_for_task_model(task)
        if submission.experiment_id:
            experiment = await get_experiment_by_id_or_name(
                session, submission.experiment_id, org_id
            )
            if not experiment:
                experiment = await get_or_create_experiment(
                    session, submission.experiment_id, org_id
                )
            new_experiment_id = experiment.id
        elif primary_experiment is not None:
            experiment = primary_experiment
        else:
            # Task was uploaded via ``oddish upload`` (or otherwise
            # landed in the DB without any trials) and therefore has no
            # linked experiment yet. Auto-create one here so the user
            # can run trials against an upload-only task without having
            # to pass ``--experiment`` explicitly -- mirroring plain
            # ``oddish run`` which also auto-generates an experiment
            # when none is supplied.
            from oddish.experiment import generate_experiment_name

            experiment = await get_or_create_experiment(
                session, generate_experiment_name(), org_id
            )
            new_experiment_id = experiment.id

        # Determine default environment from existing trial, if present.
        existing_env_result = await session.execute(
            select(TrialModel.environment)
            .where(
                TrialModel.task_id == task.id,
                TrialModel.environment.is_not(None),
            )
            .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
            .limit(1)
        )
        existing_environment = existing_env_result.scalar_one_or_none()
        effective_default_env = (
            EnvironmentType(existing_environment)
            if existing_environment
            else default_environment
        )

        target_experiment_id = new_experiment_id or (
            primary_experiment.id if primary_experiment else None
        )
        existing_counts: dict[tuple[str, str | None], int] | None = None
        if task.current_version_id is not None:
            reconcile_where = [
                TrialModel.task_id == task.id,
                TrialModel.task_version_id == task.current_version_id,
                TrialModel.is_probe.is_(False),
            ]
            if target_experiment_id is not None:
                reconcile_where.append(TrialModel.experiment_id == target_experiment_id)
            existing_counts_result = await session.execute(
                select(TrialModel.agent, TrialModel.model, func.count(TrialModel.id))
                .where(*reconcile_where)
                .group_by(TrialModel.agent, TrialModel.model)
            )
            existing_counts = {
                (agent, model): count
                for agent, model, count in existing_counts_result.all()
            }

        trials = build_trial_specs_from_sweep(
            submission,
            default_environment=effective_default_env,
            allowed_environments=allowed_environments,
            existing_counts=existing_counts,
        )

        append_submission = submission.model_copy(
            update={
                "name": task.name,
                "priority": task.priority,
                "experiment_id": target_experiment_id,
                "tags": task.tags or {},
                "run_analysis": task.run_analysis,
                "run_probe": task.run_probe,
                "user": task.user,
            }
        )
        expanded = build_task_submission_from_sweep(
            append_submission, task_path=task.task_path, trials=trials
        )
        new_trials = await append_trials_to_task(
            session,
            task=task,
            submission=expanded,
            experiment_id=new_experiment_id,
        )

        # Local dev: when ODDISH_LOCAL_MODE=1, dispatch each probe trial
        # to the in-process runner instead of going through the Modal queue.
        from oddish.config import settings

        if settings.local_mode:
            import asyncio
            from oddish.worker.local_runner import run_trial_locally

            for trial in new_trials:
                asyncio.create_task(run_trial_locally(trial.id, dry_run=False))

        if task.run_probe:
            await maybe_enqueue_auto_probe(
                session,
                task=task,
                experiment=experiment,
                org_id=org_id,
                registry_auth=submission.registry_auth,
            )
        if (
            reservation is not None
            and idempotency_store is not None
            and org_id is not None
        ):
            # Flush so trial ids / timestamps are populated, then store the
            # response for replay alongside the trials in this transaction.
            await session.flush()
            await idempotency_store.complete(
                org_id,
                SWEEP_ROUTE,
                reservation.key_hash,
                build_task_sweep_response(
                    task, new_trials, True, experiment
                ).model_dump(mode="json"),
            )
        return task, new_trials, True, experiment

    # Create mode
    task_path, task_s3_key = await resolve_task_storage(
        submission.task_id,
        s3_missing_detail=(
            f"Task {submission.task_id} not found in S3. "
            "Upload it first with POST /tasks/upload/init and POST /tasks/upload/complete"
        ),
        local_missing_detail=(
            f"Task {submission.task_id} not found in local storage. "
            "Direct task uploads require S3-backed storage"
        ),
    )
    trials = build_trial_specs_from_sweep(
        submission,
        default_environment=default_environment,
        allowed_environments=allowed_environments,
    )
    expanded = build_task_submission_from_sweep(
        submission, task_path=task_path, trials=trials
    )

    try:
        task = await create_task(
            session, expanded, task_id=submission.task_id, org_id=org_id
        )
    except TaskTimeoutValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task_s3_key:
        task.task_s3_key = task_s3_key

    experiment = await _primary_experiment_for_task_model(task)

    new_trials = list(task.trials)

    # Local dev: when ODDISH_LOCAL_MODE=1, dispatch each probe trial
    # to the in-process runner instead of going through the Modal queue.
    from oddish.config import settings

    if settings.local_mode:
        import asyncio
        from oddish.worker.local_runner import run_trial_locally

        for trial in new_trials:
            asyncio.create_task(run_trial_locally(trial.id, dry_run=False))

    if task.run_probe:
        await maybe_enqueue_auto_probe(
            session,
            task=task,
            experiment=experiment,
            org_id=org_id,
            registry_auth=submission.registry_auth,
        )
    if reservation is not None and idempotency_store is not None and org_id is not None:
        # Flush so trial ids / timestamps are populated, then store the
        # response for replay alongside the trials in this transaction.
        await session.flush()
        await idempotency_store.complete(
            org_id,
            SWEEP_ROUTE,
            reservation.key_hash,
            build_task_sweep_response(task, new_trials, False, experiment).model_dump(
                mode="json"
            ),
        )
    return task, new_trials, False, experiment


async def create_task_sweep_batch_core(
    session: AsyncSession,
    *,
    submissions: Sequence[TaskSweepSubmission],
    org_id: str | None = None,
    default_environment: EnvironmentType | None = None,
    allowed_environments: Collection[EnvironmentType] | None = None,
    prepare: (
        Callable[[AsyncSession, TaskSweepSubmission], Awaitable[EnvironmentType | None]]
        | None
    ) = None,
    finalize: (
        Callable[
            [
                AsyncSession,
                TaskSweepSubmission,
                TaskModel,
                bool,
                ExperimentModel | None,
            ],
            Awaitable[None],
        ]
        | None
    ) = None,
) -> list[TaskSweepBatchItemResult]:
    """Create several task sweeps in one transaction, best-effort.

    Each submission runs inside its own SAVEPOINT (``session.begin_nested()``):
    if an item fails, only that item is rolled back, leaving sibling items -- and
    the rows they already inserted -- intact. The caller commits the outer
    transaction once after this returns. Returns a per-item result list aligned
    to ``submissions`` by ``index`` (best-effort / 207-style semantics).

    Per-item creation reuses :func:`create_task_sweep_core`, so the same
    single-statement bulk insert of trials and worker jobs (see
    ``oddish.queue._bulk_insert_trials`` / ``bulk_enqueue_worker_jobs``) is used
    here as on the single-sweep path.

    ``prepare`` (optional) runs inside each item's savepoint before creation and
    returns the default environment for that submission; it is where a caller
    performs per-item, auth-aware setup (identity resolution, attribution).
    ``finalize`` (optional) runs inside the savepoint after creation for
    post-create stamping. Keeping both inside the savepoint preserves per-item
    atomicity -- a failure in either rolls back just that item.

    Per-item idempotency-key replay is intentionally out of scope: this path
    calls :func:`create_task_sweep_core` without idempotency arguments, so batch
    items are not deduplicated server-side the way the single ``/tasks/sweep``
    route is.
    """
    from oddish.core.sweeps import validate_sweep_submission

    results: list[TaskSweepBatchItemResult] = []
    for index, submission in enumerate(submissions):
        try:
            async with session.begin_nested():
                validate_sweep_submission(submission)
                item_default_env = default_environment
                if prepare is not None:
                    item_default_env = await prepare(session, submission)
                task, new_trials, is_append, experiment = await create_task_sweep_core(
                    session,
                    submission=submission,
                    org_id=org_id,
                    default_environment=item_default_env,
                    allowed_environments=allowed_environments,
                )
                if finalize is not None:
                    await finalize(session, submission, task, is_append, experiment)
        except HTTPException as exc:
            # Expected validation/lookup failures (e.g. missing task -> 404).
            results.append(
                TaskSweepBatchItemResult(
                    index=index,
                    success=False,
                    status_code=exc.status_code,
                    error=str(exc.detail),
                )
            )
        except Exception as exc:  # noqa: BLE001 - per-item isolation is the contract
            # Any other error is contained to this item; the savepoint has been
            # rolled back, so the session stays usable for the remaining items.
            results.append(
                TaskSweepBatchItemResult(
                    index=index,
                    success=False,
                    status_code=400,
                    error=str(exc),
                )
            )
        else:
            results.append(
                TaskSweepBatchItemResult(
                    index=index,
                    success=True,
                    status_code=200,
                    task=build_task_sweep_response(
                        task, new_trials, is_append, experiment
                    ),
                )
            )
    return results
