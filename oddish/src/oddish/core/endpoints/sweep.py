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


def _stamp_experiment_provenance(
    experiment: ExperimentModel | None,
    submission: TaskSweepSubmission,
) -> None:
    """Record the creating run's owner + PR link on the experiment (set-once).

    ``submission.github_username`` / ``submission.user`` identify whoever
    launched the run (resolved to the authenticated actor upstream), and
    ``submission.link`` is the PR/run URL. We only set each field when it is
    still empty, so the experiment reflects the run that *created* it and a
    later run of a shared task never overwrites it. Tasks keep their own
    (mutable) ``link``; this is the durable per-experiment copy.
    """
    if experiment is None:
        return
    owner = submission.github_username or submission.user
    if not experiment.owner and owner:
        experiment.owner = owner
    if not experiment.link and submission.link:
        experiment.link = submission.link


async def _finalize_sweep(
    session: AsyncSession,
    *,
    task: TaskModel,
    new_trials: list[TrialModel],
    experiment: ExperimentModel | None,
    is_append: bool,
    org_id: str | None,
    billed_user_id: str | None,
    registry_auth,
    reservation: Reservation | None,
    idempotency_store: IdempotencyStore | None,
) -> None:
    """Shared finalize tail for the append and create sweep branches.

    Behavior-identical for both branches: local-mode dispatch, auto-probe
    enqueue, and idempotency completion. ``is_append`` only selects the
    response shape stored for replay.
    """
    from oddish.config import settings
    from oddish.core.probe.auto_probe import maybe_enqueue_auto_probe

    # Local dev: when ODDISH_LOCAL_MODE=1, dispatch each probe trial
    # to the in-process runner instead of going through the Modal queue.
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
            billed_user_id=billed_user_id,
            registry_auth=registry_auth,
        )
    if reservation is not None and idempotency_store is not None and org_id is not None:
        # Flush so trial ids / timestamps are populated, then store the
        # response for replay alongside the trials in this transaction.
        await session.flush()
        await idempotency_store.complete(
            org_id,
            SWEEP_ROUTE,
            reservation.key_hash,
            build_task_sweep_response(
                task, new_trials, is_append, experiment
            ).model_dump(mode="json"),
        )


async def create_task_sweep_core(
    session: AsyncSession,
    *,
    submission: TaskSweepSubmission,
    org_id: str | None = None,
    billed_user_id: str | None = None,
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
        _ensure_not_collection_target,
        append_trials_to_task,
        create_task,
        get_experiment_by_id_or_name,
        get_or_create_experiment,
    )
    from oddish.core.tasks import resolve_task_storage
    from oddish.task_timeouts import TaskTimeoutValidationError
    from oddish.core.quota_admission import admit_trials

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
            try:
                _ensure_not_collection_target(experiment)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
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

        # Stamp the run's owner + PR link onto the experiment (set-once).
        _stamp_experiment_provenance(experiment, submission)

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
        await admit_trials(
            session, org_id, billed_user_id, count=len(expanded.trials)
        )
        new_trials = await append_trials_to_task(
            session,
            task=task,
            submission=expanded,
            experiment_id=new_experiment_id,
            billed_user_id=billed_user_id,
        )

        await _finalize_sweep(
            session,
            task=task,
            new_trials=new_trials,
            experiment=experiment,
            is_append=True,
            org_id=org_id,
            billed_user_id=billed_user_id,
            registry_auth=submission.registry_auth,
            reservation=reservation,
            idempotency_store=idempotency_store,
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

    await admit_trials(session, org_id, billed_user_id, count=len(expanded.trials))

    try:
        task = await create_task(
            session,
            expanded,
            task_id=submission.task_id,
            org_id=org_id,
            billed_user_id=billed_user_id,
        )
    except TaskTimeoutValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task_s3_key:
        task.task_s3_key = task_s3_key

    experiment = await _primary_experiment_for_task_model(task)

    # Stamp the run's owner + PR link onto the experiment (set-once).
    _stamp_experiment_provenance(experiment, submission)

    new_trials = list(task.trials)

    await _finalize_sweep(
        session,
        task=task,
        new_trials=new_trials,
        experiment=experiment,
        is_append=False,
        org_id=org_id,
        billed_user_id=billed_user_id,
        registry_auth=submission.registry_auth,
        reservation=reservation,
        idempotency_store=idempotency_store,
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
    resolve_billed_user_id: (
        Callable[[AsyncSession, TaskSweepSubmission], Awaitable[str | None]] | None
    ) = None,
) -> list[TaskSweepBatchItemResult]:
    """Create several task sweeps in one transaction, best-effort.

    Each submission runs inside its own SAVEPOINT, so a failing item rolls back
    alone. A read-only pre-loop resolves each item's environment and billed
    user; a failure there becomes that item's result. Per-item creation reuses
    ``create_task_sweep_core`` without idempotency arguments, so batch items are
    not deduplicated the way the single route is.
    """
    from oddish.core.sweeps import validate_sweep_submission

    def _failure(index: int, exc: Exception) -> TaskSweepBatchItemResult:
        if isinstance(exc, HTTPException):
            detail = exc.detail
            error = (
                detail["message"]
                if isinstance(detail, dict) and "message" in detail
                else str(detail)
            )
            return TaskSweepBatchItemResult(
                index=index, success=False, status_code=exc.status_code, error=error
            )
        return TaskSweepBatchItemResult(
            index=index, success=False, status_code=400, error=str(exc)
        )

    pre_failures: dict[int, TaskSweepBatchItemResult] = {}
    item_envs: list[EnvironmentType | None] = [default_environment] * len(submissions)
    billed_user_ids: list[str | None] = [None] * len(submissions)
    for index, submission in enumerate(submissions):
        try:
            if prepare is not None:
                item_envs[index] = await prepare(session, submission)
            if resolve_billed_user_id is not None:
                billed_user_ids[index] = await resolve_billed_user_id(
                    session, submission
                )
        except Exception as exc:  # noqa: BLE001 - per-item isolation is the contract
            pre_failures[index] = _failure(index, exc)

    results: list[TaskSweepBatchItemResult] = []
    for index, submission in enumerate(submissions):
        if index in pre_failures:
            results.append(pre_failures[index])
            continue
        try:
            async with session.begin_nested():
                validate_sweep_submission(submission)
                task, new_trials, is_append, experiment = await create_task_sweep_core(
                    session,
                    submission=submission,
                    org_id=org_id,
                    billed_user_id=billed_user_ids[index],
                    default_environment=item_envs[index],
                    allowed_environments=allowed_environments,
                )
                if finalize is not None:
                    await finalize(session, submission, task, is_append, experiment)
        except Exception as exc:  # noqa: BLE001 - per-item isolation is the contract
            # The savepoint has been rolled back, so the session stays usable
            # for the remaining items.
            results.append(_failure(index, exc))
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
