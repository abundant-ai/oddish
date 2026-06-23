from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_policy import (
    ALLOWED_CLOUD_ENVIRONMENTS,
    get_default_cloud_environment,
)
from oddish.dispatch.backends.modal import ModalDispatcher
from oddish.dispatch.ports import WorkerHandle
from oddish.core.endpoints import (
    browse_tasks_core,
    build_task_sweep_response,
    cancel_task_qa_core,
    combine_experiments_core,
    create_task_sweep_batch_core,
    create_task_sweep_core,
    delete_experiment_core,
    get_task_detail_core,
    get_task_for_org_core,
    get_task_status_core,
    get_task_version_core,
    list_tasks_core,
    list_task_versions_core,
    rerun_task_qa_core,
)
from oddish.core.dashboard import (
    EXPERIMENTS_UNATTRIBUTED_OWNER,
    invalidate_dashboard_cache,
)
from oddish.core.experiments import (
    list_experiment_probes_core,
    list_org_probes_core,
)
from oddish.core.public_helpers import (
    ensure_experiment_public,
    get_task_file_content_s3,
    list_task_files_s3,
)
from oddish.core.idempotency import IdempotencyReplay, compute_request_hash
from idempotency_store import SubmissionIdempotencyStore
from api.schemas import (
    ExperimentShareResponse,
    ExperimentUpdateRequest,
    ExperimentUpdateResponse,
)
from auth import APIKeyScope, AuthContext, require_admin, require_auth
from dashboard_attribution import resolve_search_authors
from models import APIKeyModel, UserModel
from oddish.core.tasks import (
    complete_task_upload,
    initialize_task_upload,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    get_session,
)
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now
from oddish.queue import (
    cancel_tasks_runs,
)
from oddish.schemas import (
    ExperimentCombineRequest,
    ExperimentCombineResponse,
    ExperimentProbeRow,
    OrgProbeRow,
    TaskBrowseResponse,
    TaskBatchCancelRequest,
    TaskDetailResponse,
    TaskUploadCompleteRequest,
    TaskUploadInitRequest,
    TaskUploadInitResponse,
    TaskResponse,
    TaskStatusResponse,
    TaskSweepBatchRequest,
    TaskSweepBatchResponse,
    TaskSweepSubmission,
    TaskVersionResponse,
    UploadResponse,
)

router = APIRouter(tags=["Tasks"])
logger = logging.getLogger(__name__)


def _make_timing_recorder(request: Request) -> TimingRecorder:
    def _record(name: str, duration_ms: float, description: str | None = None) -> None:
        add_server_timing_metric(request, name, duration_ms, description)

    return _record


def _split_tag_csv(csv: str | None) -> list[str]:
    return [s.strip() for s in (csv or "").split(",") if s.strip()]


async def _cancel_modal_function_calls(modal_fc_ids: list[str]) -> int:
    """Terminate in-flight Modal worker containers by function-call id.

    Resolves the persisted handles to the registered ``ModalDispatcher`` rather
    than reaching into ``modal.FunctionCall`` here, so the control-plane cancel
    is host-agnostic (design spec §6.4). Behavior is unchanged — the dispatcher
    runs the same batched ``cancel.aio(terminate_containers=True)``.
    """
    handles = [
        WorkerHandle(provider=ModalDispatcher.name, queue_key="", id=fc_id)
        for fc_id in modal_fc_ids
        if fc_id
    ]
    return await ModalDispatcher().cancel(handles)


def _apply_github_attribution(submission: TaskSweepSubmission) -> None:
    if submission.github_username:
        submission.tags = submission.tags or {}
        submission.tags.setdefault("github_username", submission.github_username)


async def _resolve_actor_user(
    session: AsyncSession,
    auth: AuthContext,
) -> UserModel | None:
    """Return the UserModel of the authenticating principal, or None.

    The auth dependency caches lightweight identity tuples — on cache hits
    the ORM ``user`` / ``api_key`` objects are stripped and only the IDs are
    available, so we lazy-load via ``session.get`` when needed.
    """
    if auth.user is not None:
        return auth.user
    if auth.user_id:
        user = await session.get(UserModel, auth.user_id)
        if user is not None:
            return user
    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return await session.get(UserModel, api_key.created_by_user_id)
    return None


async def _resolve_actor_user_string(
    session: AsyncSession,
    auth: AuthContext,
    explicit_user: str | None,
    explicit_github_username: str | None,
) -> str:
    """Resolve a non-empty author string from the authenticated actor.

    Precedence:
      1. explicit_user (e.g. --user)
      2. explicit_github_username (e.g. --github-user)
      3. actor's UserModel.email (the stable Clerk-backed identity)
      4. api_key.name (service-account API keys with no linked user)
      5. "unknown" (so tasks.user is never empty)
    """
    if explicit_user:
        return explicit_user
    if explicit_github_username:
        return explicit_github_username

    actor = await _resolve_actor_user(session, auth)
    if actor and actor.email:
        return actor.email

    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.name:
            return api_key.name

    return "unknown"


async def _resolve_submission_identity(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    """Fill submission.user and submission.github_username from the authenticated
    actor when missing. Mutates submission in place.

    `github_username` is only auto-filled from UserModel.github_username so the
    dashboard's `source: "github"` attribution stays meaningful.
    """
    if not submission.github_username:
        actor = await _resolve_actor_user(session, auth)
        if actor and actor.github_username:
            submission.github_username = actor.github_username

    submission.user = await _resolve_actor_user_string(
        session,
        auth,
        explicit_user=submission.user,
        explicit_github_username=submission.github_username,
    )


async def _lookup_user_by_github_username(
    session: AsyncSession,
    *,
    github_username: str,
    org_id: str,
) -> UserModel | None:
    normalized = (github_username or "").strip().lstrip("@")
    if not normalized:
        return None
    user_result = await session.execute(
        select(UserModel).where(
            func.lower(UserModel.github_username) == normalized.lower(),
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
        )
    )
    return user_result.scalar_one_or_none()


async def _lookup_users_by_github_username(
    session: AsyncSession,
    *,
    github_username: str,
    org_id: str,
) -> list[UserModel]:
    """Plural sibling of ``_lookup_user_by_github_username``.

    Two active members can share a GitHub handle, so search filters must
    union *all* matches rather than assume a single owner. Uses
    ``scalars().all()`` (not ``scalar_one_or_none()``, which raises on
    duplicates) and reuses the same ``@``-strip + case-insensitive,
    org-scoped, active-only normalization as the singular lookup.
    """
    normalized = (github_username or "").strip().lstrip("@")
    if not normalized:
        return []
    result = await session.execute(
        select(UserModel).where(
            func.lower(UserModel.github_username) == normalized.lower(),
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
        )
    )
    return list(result.scalars().all())


async def _resolve_created_by_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    """Who submitted the task (API key owner wins for CI/service accounts)."""
    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    if submission.github_username:
        user = await _lookup_user_by_github_username(
            session,
            github_username=submission.github_username,
            org_id=auth.org_id,
        )
        if user:
            return user.id

    if auth.user_id:
        return auth.user_id

    return None


async def _resolve_experiment_owner_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    """Primary experiment owner for dashboard Mine (GitHub author beats submitter)."""
    if submission.github_username:
        user = await _lookup_user_by_github_username(
            session,
            github_username=submission.github_username,
            org_id=auth.org_id,
        )
        if user:
            return user.id
        # Explicit --github-user with no linked org member: leave owner unset so
        # the legacy primary-task Mine filter can match the github tag.
        return None

    if auth.user_id:
        return auth.user_id

    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    return None


def _stamp_experiment_owner(
    experiment: ExperimentModel | None,
    owner_user_id: str | None,
    *,
    claim_unowned: bool = True,
) -> None:
    """Stamp the dashboard Mine owner on an experiment.

    ``claim_unowned=False`` (append/rerun path) replaces only the sweep's
    ``__unattributed__`` sentinel: a NULL owner means the sweep has not yet
    attributed the experiment's primary task, and the appender is not
    necessarily that author — claiming NULL here would race the sweep's
    precedence-correct claim and hide the experiment from its real owner.
    """
    if experiment is None or not owner_user_id:
        return
    claimable = (
        (None, EXPERIMENTS_UNATTRIBUTED_OWNER)
        if claim_unowned
        else (EXPERIMENTS_UNATTRIBUTED_OWNER,)
    )
    if experiment.owner_user_id in claimable:
        experiment.owner_user_id = owner_user_id


async def _maybe_publish_experiment(
    session: AsyncSession,
    task: TaskModel,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    should_publish = submission.publish_experiment
    if should_publish is None:
        should_publish = bool(submission.github_username and auth.api_key_id)
    if not should_publish:
        return

    experiments = list(task.experiments or [])
    for experiment in experiments:
        await ensure_experiment_public(session, experiment)


# =============================================================================
# Task Upload and Creation
# =============================================================================


@router.post("/tasks/upload/init", response_model=TaskUploadInitResponse)
async def init_task_upload(
    payload: TaskUploadInitRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskUploadInitResponse:
    """Prepare a task upload and return a presigned PUT URL when S3 is enabled."""
    auth.require_scope(APIKeyScope.TASKS)
    return await initialize_task_upload(
        payload.name,
        org_id=auth.org_id,
        content_hash=payload.content_hash,
        message=payload.message,
        force_new_version=payload.force_new_version,
    )


@router.post("/tasks/upload/complete", response_model=UploadResponse)
async def finalize_task_upload(
    payload: TaskUploadCompleteRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> UploadResponse:
    """Finalize a direct task upload after the client PUTs the archive to S3."""
    auth.require_scope(APIKeyScope.TASKS)

    resolved_user = payload.user
    if payload.register_task and not resolved_user:
        async with get_session() as session:
            resolved_user = await _resolve_actor_user_string(
                session,
                auth,
                explicit_user=payload.user,
                explicit_github_username=None,
            )

    return await complete_task_upload(
        task_id=payload.task_id,
        task_name=payload.name,
        version=payload.version,
        content_hash=payload.content_hash,
        message=payload.message,
        org_id=auth.org_id,
        created_by_user_id=auth.user_id,
        register=payload.register_task,
        user=resolved_user,
        priority=payload.priority,
    )


async def _apply_user_run_probe_default(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    """Opt the creating user's NEW tasks into auto-probe per their default.

    Only turns ``run_probe`` ON (an explicit ``run_probe=True`` already wins, so
    we skip the lookup then) and only matters for task creation — append mode in
    ``create_task_sweep_core`` preserves the existing task's flag, so a flipped
    submission flag is a no-op there. Resolved in the backend because the
    ``users`` table is a backend concept the oddish core must not import.
    """
    if submission.run_probe:
        return
    actor = await _resolve_actor_user(session, auth)
    if actor is not None and actor.run_probe_default:
        submission.run_probe = True


@router.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(
    submission: TaskSweepSubmission,
    auth: Annotated[AuthContext, Depends(require_auth)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskResponse:
    """Submit a task sweep - expands a task_id into many trials.

    A retried submission carrying the same ``Idempotency-Key`` replays the
    original response instead of creating duplicate trials.
    """
    auth.require_scope(APIKeyScope.TASKS)

    from oddish.core.sweeps import validate_sweep_submission

    validate_sweep_submission(submission)

    # Fingerprint the raw client submission BEFORE the backend mutates it
    # (identity / GitHub attribution / per-user probe default). Those defaults
    # can resolve differently between attempts, so hashing post-mutation would
    # spuriously 409 an honest retry; hashing the raw body keeps retries faithful.
    request_hash = compute_request_hash(submission)

    async with get_session() as session:
        await _resolve_submission_identity(session, submission, auth)
        _apply_github_attribution(submission)
        await _apply_user_run_probe_default(session, submission, auth)

        try:
            task, new_trials, is_append, experiment = await create_task_sweep_core(
                session,
                submission=submission,
                org_id=auth.org_id,
                default_environment=get_default_cloud_environment(submission),
                allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
                idempotency_key=idempotency_key,
                idempotency_store=SubmissionIdempotencyStore(session),
                request_hash=request_hash,
            )
        except IdempotencyReplay as replay:
            # Faithful retry of a completed key: return the stored response and
            # skip the owner-stamping / publish side effects below.
            return TaskResponse.model_validate(replay.response_json)

        owner_user_id = await _resolve_experiment_owner_user_id(
            session, submission, auth
        )
        _stamp_experiment_owner(experiment, owner_user_id, claim_unowned=not is_append)

        if not is_append:
            created_by_user_id = await _resolve_created_by_user_id(
                session, submission, auth
            )
            if created_by_user_id:
                task.created_by_user_id = created_by_user_id

            await _maybe_publish_experiment(session, task, submission, auth)

        elif experiment and submission.publish_experiment:
            await ensure_experiment_public(session, experiment)

        await session.commit()

        return build_task_sweep_response(task, new_trials, is_append, experiment)


@router.post("/tasks/sweep/batch", response_model=TaskSweepBatchResponse)
async def create_task_sweep_batch(
    payload: TaskSweepBatchRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
    response: Response,
) -> TaskSweepBatchResponse:
    """Submit several task sweeps in one request (best-effort, per-item status).

    Each submission is created inside its own savepoint, so one bad item neither
    aborts the batch nor rolls back items that already succeeded. ``results`` is
    a per-item status array indexed to ``submissions``. Returns HTTP 200 when
    every item succeeds and HTTP 207 Multi-Status when at least one item fails --
    callers must inspect each item's ``success``/``status_code``.

    Per-item idempotency-key replay is intentionally not handled here; request
    idempotency is separate in-flight work and will layer on top of this path.
    """
    auth.require_scope(APIKeyScope.TASKS)

    if not payload.submissions:
        raise HTTPException(
            status_code=400, detail="Must specify at least one submission"
        )

    async def _prepare(
        session: AsyncSession, submission: TaskSweepSubmission
    ) -> EnvironmentType | None:
        # Per-item, auth-aware setup. Runs inside the item's savepoint so a
        # failure here rolls back only this item (mirrors the single-sweep route).
        await _resolve_submission_identity(session, submission, auth)
        _apply_github_attribution(submission)
        await _apply_user_run_probe_default(session, submission, auth)
        return get_default_cloud_environment(submission)

    async def _finalize(
        session: AsyncSession,
        submission: TaskSweepSubmission,
        task: TaskModel,
        is_append: bool,
        experiment: ExperimentModel | None,
    ) -> None:
        # Post-create stamping, inside the savepoint (mirrors the single route).
        owner_user_id = await _resolve_experiment_owner_user_id(
            session, submission, auth
        )
        _stamp_experiment_owner(experiment, owner_user_id, claim_unowned=not is_append)
        if not is_append:
            created_by_user_id = await _resolve_created_by_user_id(
                session, submission, auth
            )
            if created_by_user_id:
                task.created_by_user_id = created_by_user_id
            await _maybe_publish_experiment(session, task, submission, auth)
        elif experiment and submission.publish_experiment:
            await ensure_experiment_public(session, experiment)

    async with get_session() as session:
        results = await create_task_sweep_batch_core(
            session,
            submissions=payload.submissions,
            org_id=auth.org_id,
            allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
            prepare=_prepare,
            finalize=_finalize,
        )
        await session.commit()

    succeeded = sum(1 for r in results if r.success)
    failed = len(results) - succeeded
    # 207 Multi-Status whenever any item failed; the body carries per-item
    # outcomes so the client never has to rely on the top-level status alone.
    if failed:
        response.status_code = status.HTTP_207_MULTI_STATUS
    return TaskSweepBatchResponse(
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


# =============================================================================
# Task Listing and Retrieval
# =============================================================================


@router.get("/tasks", response_model=list[TaskStatusResponse])
async def list_tasks(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    status: str | None = None,
    user: str | None = None,
    experiment_id: str | None = None,
    include_trials: bool = False,
    compact_trials: bool = False,
    compact_tasks: bool = False,
    include_queue_info: bool = True,
    include_worker_jobs: bool = True,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """List tasks for the authenticated organization.

    ``compact_tasks=true`` is a fast-path used by the experiment page
    first paint: it implies ``include_trials=false`` and skips the
    per-task ``visible_worker_jobs`` and ``effective_version_ids``
    lookups. The phase-2 batched fetch (``include_trials=true``) fills
    those columns in afterwards.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Tasks DB connect",
        )
        tasks = await list_tasks_core(
            session,
            status=status,
            user=user,
            experiment_id=experiment_id,
            include_trials=include_trials,
            compact_trials=compact_trials,
            compact_tasks=compact_tasks,
            include_queue_info=include_queue_info,
            include_worker_jobs=include_worker_jobs,
            limit=limit,
            offset=offset,
            org_id=auth.org_id,
            include_empty_rewards=True,
            record_timing=_make_timing_recorder(request),
        )
        return tasks


@router.get("/tasks/browse", response_model=TaskBrowseResponse)
async def browse_tasks(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str | None = None,
    tags: str | None = Query(None),
    tags_any: str | None = Query(None),
    tags_none: str | None = Query(None),
    author: str | None = Query(
        None,
        description=(
            "Author search (the github:/author:/user: qualifier). Comma-separated "
            "tokens, each resolved to matching org members + their aliases and "
            "ANDed with the free-text and tag filters."
        ),
    ),
) -> TaskBrowseResponse:
    """Browse latest task versions for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Browse DB connect",
        )
        author_tokens = [
            token.strip() for token in (author or "").split(",") if token.strip()
        ]
        if author_tokens:
            (
                author_user_ids,
                author_github_usernames,
                author_emails,
            ) = await resolve_search_authors(
                session, org_id=auth.org_id, tokens=author_tokens
            )
        else:
            author_user_ids = ()
            author_github_usernames = ()
            author_emails = ()
        return await browse_tasks_core(
            session,
            org_id=auth.org_id,
            limit=limit,
            offset=offset,
            query=query,
            tags_all=_split_tag_csv(tags),
            tags_any=_split_tag_csv(tags_any),
            tags_none=_split_tag_csv(tags_none),
            author_user_ids=author_user_ids,
            author_github_usernames=author_github_usernames,
            author_emails=author_emails,
            record_timing=_make_timing_recorder(request),
        )


@router.post("/experiments/combine", response_model=ExperimentCombineResponse)
async def combine_experiments(
    payload: ExperimentCombineRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ExperimentCombineResponse:
    """Combine several experiments into a new result experiment.

    Creates a brand-new experiment and copies the task memberships and
    finished trials (with their S3 artifacts) of every source experiment
    into it. The sources are org-scoped and left untouched; append-only,
    so this needs only the ``tasks`` scope rather than admin.
    """
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        result = await combine_experiments_core(
            session,
            source_experiment_ids=payload.source_experiment_ids,
            name=payload.name,
            org_id=auth.org_id,
            copy_artifacts=payload.copy_artifacts,
        )
        await session.commit()

    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.get(
    "/experiments/{experiment_id}/share", response_model=ExperimentShareResponse
)
async def get_experiment_share(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ExperimentShareResponse:
    """Get share status for an experiment."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=bool(experiment.is_public),
            public_token=experiment.public_token,
            description=experiment.description,
        )


@router.patch(
    "/experiments/{experiment_id}",
    response_model=ExperimentUpdateResponse,
)
async def update_experiment(
    experiment_id: str,
    payload: ExperimentUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentUpdateResponse:
    """Update experiment metadata.

    ``name`` and ``description`` are independently optional: a request may
    update either or both. Only fields explicitly provided (``not None``) are
    touched, so a description edit never clobbers the name and vice versa.
    """
    if payload.name is None and payload.description is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    name: str | None = None
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(
                status_code=400, detail="Experiment name cannot be empty"
            )

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        if name is not None:
            experiment.name = name
        if payload.description is not None:
            # Treat blank/whitespace-only as "no description" so the empty
            # state is uniform (NULL) regardless of how it was cleared.
            cleaned = payload.description.strip()
            experiment.description = cleaned or None
        await session.commit()

        return ExperimentUpdateResponse(
            id=experiment.id,
            name=experiment.name,
            description=experiment.description,
        )


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Soft-delete an experiment and its experiment-scoped data.

    This tombstones the experiment plus its scoped trials and any tasks
    orphaned by removing the experiment membership. Artifacts remain in
    storage; the core path returns an empty ``s3_prefixes`` list so the
    API layer performs no hard-deletion follow-up.
    """
    async with get_session() as session:
        result = await delete_experiment_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)

    return result


@router.post(
    "/experiments/{experiment_id}/publish",
    response_model=ExperimentShareResponse,
)
async def publish_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentShareResponse:
    """Publish an experiment for public read-only access."""

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        await ensure_experiment_public(session, experiment)
        await session.commit()

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=True,
            public_token=experiment.public_token,
        )


@router.post(
    "/experiments/{experiment_id}/unpublish",
    response_model=ExperimentShareResponse,
)
async def unpublish_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentShareResponse:
    """Unpublish an experiment (public link will stop working)."""

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        experiment = result.scalar_one_or_none()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        experiment.is_public = False
        await session.commit()

        return ExperimentShareResponse(
            name=experiment.name,
            is_public=False,
            public_token=experiment.public_token,
        )


@router.get(
    "/experiments/{experiment_id}/probes",
    response_model=list[ExperimentProbeRow],
)
async def list_experiment_probes(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ExperimentProbeRow]:
    """List probe trials for each task in the experiment.

    Returns at most one row per task — the most recent probe trial for the
    task's current version.  Tasks with no probe trials are omitted.
    Each row includes: ``task_id``, ``task_name``, ``version``, ``model``,
    ``status``, ``probe_trial_id``.

    Raises 404 if the experiment does not exist for the authenticated org.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        result = await session.execute(
            select(ExperimentModel).where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == auth.org_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return await list_experiment_probes_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
        )


@router.get("/probes", response_model=list[OrgProbeRow])
async def list_org_probes(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[OrgProbeRow]:
    """List the authenticated org's tasks that have probe runs.

    One row per task with at least one probe trial — task id/name, total
    probe-run count, and the timestamp + status of the most recent probe
    trial. Ordered most-recent-first.
    """
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_org_probes_core(session, org_id=auth.org_id)


@router.post("/tasks/cancel")
async def cancel_tasks(
    payload: TaskBatchCancelRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Cancel in-flight runs for many tasks without deleting data."""
    auth.require_scope(APIKeyScope.TASKS)
    if not payload.task_ids:
        raise HTTPException(status_code=400, detail="Provide at least one task_id")

    try:
        async with get_session() as session:
            result = await cancel_tasks_runs(
                session, payload.task_ids, org_id=auth.org_id
            )
            if result.get("error") == "not_found":
                raise HTTPException(status_code=404, detail="No matching tasks found")
            await session.commit()
    except SQLAlchemyError as exc:
        # Full detail goes to the logs: exc_info captures the traceback (which
        # statement raised) plus exc.statement (the SQL) and exc.orig (the
        # Postgres deadlock/timeout detail). The UI gets a simple, honest
        # message instead of an opaque "Internal Server Error".
        logger.error(
            "cancel_tasks failed for task_ids=%s", payload.task_ids, exc_info=exc
        )
        raise HTTPException(
            status_code=503,
            detail="Couldn't cancel right now (database error). Please retry.",
        ) from exc

    modal_cancelled = await _cancel_modal_function_calls(
        result.get("modal_function_call_ids", [])
    )

    return {
        "status": "cancelled",
        "task_ids": result.get("task_ids", []),
        "not_found_task_ids": result.get("not_found_task_ids", []),
        "tasks_found": result.get("tasks_found", 0),
        "tasks_cancelled": result.get("tasks_cancelled", 0),
        "trials_cancelled": result.get("trials_cancelled", 0),
        "modal_calls_cancelled": modal_cancelled,
    }


@router.post("/tasks/{task_id}/qa/retry")
async def retry_task_qa(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """(Re)run the single task-level QA job: classify every trial, then
    synthesize the task verdict."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await rerun_task_qa_core(session, task_id=task_id, org_id=auth.org_id)


@router.post("/tasks/{task_id}/qa/cancel")
async def cancel_task_qa(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Cancel a task's in-flight QA job."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        result = await cancel_task_qa_core(session, task_id=task_id, org_id=auth.org_id)

    modal_cancelled = await _cancel_modal_function_calls(
        cast("list[str]", result.get("modal_function_call_ids", []))
    )
    return {
        key: value for key, value in result.items() if key != "modal_function_call_ids"
    } | {"modal_calls_cancelled": modal_cancelled}


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    include_trials: bool = True,
) -> TaskStatusResponse:
    """Get task status with all trials for the authenticated organization."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_status_core(
            session,
            task_id=task_id,
            include_trials=include_trials,
            include_empty_rewards=True,
            org_id=auth.org_id,
        )


@router.get("/tasks/{task_id}/detail", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskDetailResponse:
    """Task detail bundle: task + trials + per-version + cost rollups."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_detail_core(session, task_id=task_id, org_id=auth.org_id)


# =============================================================================
# Task Versions
# =============================================================================


@router.get("/tasks/{task_id}/versions", response_model=list[TaskVersionResponse])
async def list_task_versions(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[TaskVersionResponse]:
    """List all versions of a task, newest first."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await list_task_versions_core(
            session, task_id=task_id, org_id=auth.org_id
        )


@router.get("/tasks/{task_id}/versions/{version}", response_model=TaskVersionResponse)
async def get_task_version(
    task_id: str,
    version: int,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskVersionResponse:
    """Get a specific version of a task."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_task_version_core(
            session, task_id=task_id, version=version, org_id=auth.org_id
        )


# =============================================================================
# Task Files (S3 Storage)
# =============================================================================


def _build_task_file_etag(archive_etag: str, file_path: str) -> str:
    """Compose an RFC 7232 weak-etag for a task-archive-served file.

    S3's ``head_object`` returns the ``ETag`` already wrapped in double
    quotes (e.g. ``'"abc123"'``); embedding that verbatim inside
    ``W/"..."`` would emit a malformed header that browsers silently
    ignore, which would defeat the whole HTTP-cache fast path. Strip
    any leading/trailing quotes before composing the wire form.
    """
    normalized = archive_etag.strip().strip('"')
    return f'W/"{normalized}:{file_path}"'


@router.get("/tasks/{task_id}/files")
async def list_task_files(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(
        True, description="Include presigned URLs for direct S3 access"
    ),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """List all files in a task's S3 directory.

    When presign=True (default), includes presigned URLs for each file,
    allowing clients to fetch content directly from S3 without additional API calls.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        task = await get_task_for_org_core(
            session,
            task_id=task_id,
            org_id=auth.org_id,
            load_current_version=True,
        )
        if version is None and task.current_version:
            version = task.current_version.version

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=version,
    )


@router.get("/tasks/{task_id}/files/{file_path:path}")
async def get_task_file_content(
    task_id: str,
    file_path: str,
    request: Request,
    response: Response,
    auth: Annotated[AuthContext, Depends(require_auth)],
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
):
    """Get content of a specific task file from S3.

    When the underlying source is a pinned task archive (immutable at a
    given version) the response carries ``ETag`` + ``Cache-Control``
    headers and honors ``If-None-Match`` with a ``304``, so the browser's
    HTTP cache covers repeated clicks on the same file.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        task = await get_task_for_org_core(
            session,
            task_id=task_id,
            org_id=auth.org_id,
            load_current_version=True,
        )
        if version is None and task.current_version:
            version = task.current_version.version

    result = await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=version,
    )

    archive_etag = result.get("archive_etag")
    if archive_etag and version is not None:
        etag_value = _build_task_file_etag(str(archive_etag), file_path)
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and etag_value in {
            h.strip() for h in if_none_match.split(",")
        }:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag_value,
                    "Cache-Control": "private, max-age=86400, immutable",
                },
            )
        response.headers["ETag"] = etag_value
        response.headers["Cache-Control"] = "private, max-age=86400, immutable"

    return result
