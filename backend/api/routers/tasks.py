from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, cast

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
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_policy import (
    ALLOWED_CLOUD_ENVIRONMENTS,
    get_default_cloud_environment,
)
from oddish.dispatch.backends.modal import ModalDispatcher
from oddish.dispatch.ports import WorkerHandle
from oddish.filters.trial_metrics import TrialMetricFilter
from oddish.core.endpoints import (
    backfill_task_analysis_core,
    browse_experiment_options_core,
    browse_task_facets_core,
    browse_tasks_core,
    rerun_pre_trial_audit_core,
    build_task_sweep_response,
    cancel_task_qa_core,
    combine_experiments_core,
    create_task_sweep_batch_core,
    create_task_sweep_core,
    delete_experiment_core,
    delete_task_core,
    get_experiment_cost_totals,
    get_task_detail_core,
    get_task_for_org_core,
    get_task_status_core,
    get_task_version_core,
    list_experiment_slim_tasks,
    list_experiment_task_shells_core,
    list_tasks_core,
    replay_has_retryable_failed_trials,
    list_task_versions_core,
    rerun_task_qa_core,
    set_task_default_version_core,
    unlink_task_from_experiment_core,
)
from oddish.core.helpers import terminate_run_harvest


from oddish.core.dashboard import (
    invalidate_dashboard_cache,
)
from oddish.core.experiments import (
    list_experiment_probes_core,
    list_org_probes_core,
)
from oddish.core.sharing.helpers import (
    ensure_experiment_public,
    get_task_file_content_s3,
    list_task_files_s3,
    make_task_files_ndjson_response,
    stream_task_files_s3,
)
from oddish.core.idempotency import (
    IdempotencyReplay,
    SWEEP_ROUTE,
    compute_request_hash,
    probe_completed_replay,
)
from idempotency_store import SubmissionIdempotencyStore
from api.schemas import (
    ExperimentShareResponse,
    ExperimentUpdateRequest,
    ExperimentUpdateResponse,
)
from auth import APIKeyScope, AuthContext, require_admin, require_auth
from api.routers.task_submission import (
    apply_github_attribution,
    maybe_publish_experiment,
    require_connected_github_user,
    require_experiment_publish_scope,
    resolve_actor_user_string,
    resolve_billed_user_id,
    resolve_created_by_user_id,
    resolve_experiment_owner_user_id,
    resolve_submission_identity,
    stamp_experiment_owner,
)
from dashboard_attribution import resolve_search_authors
from oddish.core.tasks import (
    complete_task_upload,
    initialize_task_upload,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    get_session,
    utcnow,
)
from oddish.timing import TimingRecorder, add_server_timing_metric, elapsed_ms, now
from oddish.queue import (
    cancel_tasks_runs,
)
from oddish.core.endpoints.collections import (
    add_to_collection_core,
    create_trial_collection_core,
    remove_from_collection_core,
    rename_collection_core,
)
from oddish.schemas import (
    BackfillQARequest,
    CollectionAddRequest,
    CollectionMutationResponse,
    CollectionRemoveRequest,
    CollectionRenameRequest,
    ExperimentCombineRequest,
    ExperimentCombineResponse,
    ExperimentCostTotals,
    ExperimentOptionsResponse,
    ExperimentProbeRow,
    OrgProbeRow,
    TaskBrowseFacets,
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
    TrialCollectionRequest,
    TrialCollectionResponse,
    UploadResponse,
)

if TYPE_CHECKING:
    from models import UserModel

router = APIRouter(tags=["Tasks"])
logger = logging.getLogger(__name__)


async def _spawn_gke_image_builds(session: AsyncSession, task_ids: list[str]) -> None:
    """Fire the upload-time image builder for GKE-classified tasks (post-commit).

    Primary build path: the worker-side auto_build_missing_image fallback only
    covers the race where a trial claims before this build lands. Best-effort
    by design -- a spawn failure must never fail a committed submission (the
    worker fallback and the clear missing-image error remain behind it).
    """
    if not task_ids:
        return
    try:
        import os

        import modal

        # Spawn by name: importing worker.functions here would re-run Modal
        # function registration inside the API container. from_name resolves
        # the deployed function directly; GKE-less deploys never register it
        # and the NotFoundError lands in the catch below.
        builder = modal.Function.from_name(
            os.environ.get("MODAL_APP_NAME", "oddish"),
            "build_gke_task_image",
            environment_name=os.environ.get("MODAL_ENVIRONMENT") or None,
        )
        from oddish.db.models import TaskModel, TaskVersionModel, TrialModel

        # Scoped to trials ON the task's current version: stale GKE trials
        # from older versions must not trigger builds for content they never
        # ran. If a concurrent submission bumps the version between commit and
        # this query, the build targets the newer content and the older
        # trials' worker-side auto-build fallback covers the gap.
        gke_rows = await session.execute(
            select(TrialModel.task_id, TaskVersionModel.version)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .join(
                TaskVersionModel,
                TaskVersionModel.id == TaskModel.current_version_id,
            )
            .where(
                TrialModel.task_id.in_(task_ids),
                TrialModel.task_version_id == TaskModel.current_version_id,
                # Environment is the routing truth: allowlisted harbor-gke
                # pins at non-blessed SHAs classify as the ephemeral variant
                # yet still run on GKE and need the prebuilt image.
                or_(
                    TrialModel.environment == "gke",
                    TrialModel.harbor_config["variant_id"].astext == "gke",
                ),
            )
            .distinct()
        )
        for task_id, version in gke_rows:
            try:
                await builder.spawn.aio(task_id=task_id, version=version)
            except modal.exception.NotFoundError:
                # GKE-less deploy: the builder function isn't registered, so
                # every remaining spawn would fail identically -- let the
                # outer catch log it once.
                raise
            except Exception:
                logger.exception(
                    "GKE image build spawn failed for task %s v%s (non-fatal)",
                    task_id,
                    version,
                )
                continue
            logger.info("spawned GKE image build for task %s v%s", task_id, version)
    except Exception:
        logger.exception("GKE image build spawn failed (non-fatal)")


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
            resolved_user = await resolve_actor_user_string(
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


@router.post("/tasks/sweep", response_model=TaskResponse)
async def create_task_sweep(
    submission: TaskSweepSubmission,
    auth: Annotated[AuthContext, Depends(require_auth)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskResponse:
    """Submit a task sweep - expands a task_id into many trials.

    A retried submission carrying the same ``Idempotency-Key`` replays the
    original response instead of creating duplicate trials while its current
    trial leaves are non-failed. Failed leaves turn the same declarative sweep
    into immutable replacement trials.
    """
    auth.require_scope(APIKeyScope.TASKS)

    from oddish.core.sweeps import validate_sweep_submission

    validate_sweep_submission(submission)

    # Fingerprint the raw client submission BEFORE the backend mutates it
    # (identity / GitHub attribution). Those defaults can resolve differently
    # between attempts, so hashing post-mutation would spuriously 409 an honest
    # retry; hashing the raw body keeps retries faithful.
    request_hash = compute_request_hash(submission)

    async with get_session() as session:
        # A COMPLETED, hash-matched, unexpired idempotency record normally
        # replays BEFORE the linkage gate: a faithful transport retry must not
        # 403 just because linked-user state changed after submission. A failed
        # current leaf is different: it makes this an intentional rerun, so it
        # falls through the current linkage/billing gates and sweep reconcile.
        if idempotency_key:
            replay_json = await probe_completed_replay(
                SubmissionIdempotencyStore(session),
                org_id=auth.org_id,
                route=SWEEP_ROUTE,
                raw_key=idempotency_key,
                request_hash=request_hash,
                now=utcnow(),
            )
            if replay_json is not None:
                if await replay_has_retryable_failed_trials(
                    session, replay_json, org_id=auth.org_id
                ):
                    # The stable CLI key normally identifies a transport replay.
                    # Once its current retry-chain leaf has failed, the same
                    # command is instead an intentional retry. Reconciliation is
                    # task-row locked, so bypassing the old reservation remains
                    # duplicate-safe under concurrent submissions.
                    idempotency_key = None
                else:
                    return TaskResponse.model_validate(replay_json)

        await resolve_submission_identity(session, submission, auth)
        apply_github_attribution(submission)

        # Unconditional linkage gate: a truthy github_id that resolves to no
        # active org user is rejected here, before any rows are written.
        connected_user = await require_connected_github_user(session, submission, auth)

        # Billing follows the resolved owner (submitted github_id/github_username,
        # github_id first), else the API-key owner / submitter. Reuse the
        # linkage-gate user so we don't re-query it.
        owner_user_id = await resolve_experiment_owner_user_id(
            session, submission, auth, connected_user
        )
        billed_user_id = await resolve_billed_user_id(
            session, submission, auth, owner_user_id=owner_user_id
        )

        try:
            task, new_trials, is_append, experiment = await create_task_sweep_core(
                session,
                submission=submission,
                org_id=auth.org_id,
                billed_user_id=billed_user_id,
                default_environment=get_default_cloud_environment(submission),
                allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
                idempotency_key=idempotency_key,
                idempotency_store=SubmissionIdempotencyStore(session),
                request_hash=request_hash,
            )
        except TimeoutError as exc:
            # Quota advisory-lock waits (and other DB wait timeouts) surface as
            # bare TimeoutError from asyncpg. Map to 503 so the CLI retries with
            # a legible message instead of an opaque "Internal Server Error".
            logger.error(
                "create_task_sweep timed out for task_id=%s org_id=%s",
                submission.task_id,
                auth.org_id,
                exc_info=exc,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Couldn't submit right now (database lock timeout). Please retry."
                ),
            ) from exc
        except SQLAlchemyError as exc:
            logger.error(
                "create_task_sweep failed for task_id=%s org_id=%s",
                submission.task_id,
                auth.org_id,
                exc_info=exc,
            )
            raise HTTPException(
                status_code=503,
                detail="Couldn't submit right now (database error). Please retry.",
            ) from exc
        except IdempotencyReplay as replay:
            # Faithful retry of a completed key: return the stored response and
            # skip the owner-stamping / publish side effects below. The image
            # build spawn IS retried though -- it is best-effort on the
            # original request and the builder is idempotent (checks the
            # registry first), so a replay is the natural recovery hook when
            # the original spawn failed.
            response = TaskResponse.model_validate(replay.response_json)
            replay_task_id = getattr(response, "id", None)
            if replay_task_id:
                await _spawn_gke_image_builds(session, [replay_task_id])
            return response

        stamp_experiment_owner(experiment, owner_user_id, claim_unowned=not is_append)

        if not is_append:
            created_by_user_id = await resolve_created_by_user_id(
                session, submission, auth, connected_user
            )
            if created_by_user_id:
                task.created_by_user_id = created_by_user_id
            task.api_key_id = auth.api_key_id

            await maybe_publish_experiment(session, task, submission, auth)

        elif experiment and submission.publish_experiment:
            require_experiment_publish_scope(auth)
            await ensure_experiment_public(session, experiment)

        await session.commit()

        await _spawn_gke_image_builds(session, [task.id])

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

    connected_users: dict[int, UserModel | None] = {}

    async def _prepare(
        session: AsyncSession, submission: TaskSweepSubmission
    ) -> EnvironmentType | None:
        # Per-item, auth-aware setup. Runs in the batch core's read-only
        # pre-loop (identity -> attribution -> billed, same order as the single
        # route); a failure fails only this item.
        await resolve_submission_identity(session, submission, auth)
        apply_github_attribution(submission)
        # Unconditional linkage gate: a truthy github_id resolving to no active
        # org user raises 403 here; the batch core catches it and fails only
        # this item (rolling back its savepoint) before any rows are written.
        connected_users[id(submission)] = await require_connected_github_user(
            session, submission, auth
        )
        return get_default_cloud_environment(submission)

    # Owner resolved once in the pre-loop (inside _resolve_billed) and reused
    # by _finalize -- same single-resolution shape as the single route.
    owners: dict[int, str | None] = {}

    async def _resolve_billed(
        session: AsyncSession, submission: TaskSweepSubmission
    ) -> str | None:
        owner_user_id = await resolve_experiment_owner_user_id(
            session, submission, auth
        )
        owners[id(submission)] = owner_user_id
        return await resolve_billed_user_id(
            session, submission, auth, owner_user_id=owner_user_id
        )

    async def _finalize(
        session: AsyncSession,
        submission: TaskSweepSubmission,
        task: TaskModel,
        is_append: bool,
        experiment: ExperimentModel | None,
    ) -> None:
        # Post-create stamping, inside the savepoint (mirrors the single route).
        # Owner was resolved once in _resolve_billed; connected_user (linkage
        # gate) is reused for created_by resolution below.
        connected_user = connected_users.get(id(submission))
        owner_user_id = owners.get(id(submission))
        stamp_experiment_owner(experiment, owner_user_id, claim_unowned=not is_append)
        if not is_append:
            created_by_user_id = await resolve_created_by_user_id(
                session, submission, auth, connected_user
            )
            if created_by_user_id:
                task.created_by_user_id = created_by_user_id
            task.api_key_id = auth.api_key_id
            await maybe_publish_experiment(session, task, submission, auth)
        elif experiment and submission.publish_experiment:
            require_experiment_publish_scope(auth)
            await ensure_experiment_public(session, experiment)

    async with get_session() as session:
        results = await create_task_sweep_batch_core(
            session,
            submissions=payload.submissions,
            org_id=auth.org_id,
            allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
            prepare=_prepare,
            finalize=_finalize,
            resolve_billed_user_id=_resolve_billed,
        )
        await session.commit()

        await _spawn_gke_image_builds(
            session,
            [r.task.id for r in results if r.success and r.task is not None],
        )

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


@router.get(
    "/experiments/{experiment_id}/task-shells",
    response_model=list[TaskStatusResponse],
)
async def list_experiment_task_shells(
    request: Request,
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(2000, ge=1, le=2000),
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """Lightweight task shells for the experiment-details first paint.

    A dedicated, trimmed alternative to ``GET /tasks?...&compact_tasks=true``
    that additionally drops the per-task ``experiments`` fan-out. The generic
    ``/tasks`` route (and ``list_tasks_core``) are intentionally left unchanged;
    only the experiment-page first paint should call this.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Task shells DB connect",
        )
        return await list_experiment_task_shells_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            limit=limit,
            offset=offset,
            include_empty_rewards=True,
            record_timing=_make_timing_recorder(request),
        )


@router.get(
    "/experiments/{experiment_id}/cost-totals",
    response_model=ExperimentCostTotals,
)
async def get_experiment_cost_totals_route(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ExperimentCostTotals:
    """The experiment's spend rollup: member-wide cost + owned "new spend".

    ``cost_*`` prices every trial the page renders (homed or gathered, the
    grid's membership); ``owned_*`` only what the experiment ran itself — the
    additive number (``core.endpoints.experiment_cost``). Deliberately wider
    than the grid routes above: those page their trials (so the page can't sum
    cost client-side without loading all of them) and scope each task to its
    current version (so they omit earlier versions, superseded retries and
    probes -- all of which were still billed). One grouped query.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_experiment_cost_totals(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )


@router.get(
    "/experiments/{experiment_id}/slim-tasks",
    response_model=list[TaskStatusResponse],
)
async def list_experiment_slim_tasks_route(
    request: Request,
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(2000, ge=1, le=2000),
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """Phase-2 grid data with SLIM per-trial payloads for the experiment page.

    Like the experiment-scoped ``GET /tasks?include_trials=true`` path, but
    each trial carries only the fields the grid renders (+ cost). Heavy
    per-trial detail is fetched on demand via ``GET /trials/{trial_id}`` when a
    cell is clicked. The generic ``/tasks`` route is left unchanged; only the
    experiment-page Phase-2 fetch should call this.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        connect_started_at = now()
        await session.connection()
        add_server_timing_metric(
            request,
            "db_connect",
            elapsed_ms(connect_started_at),
            "Slim tasks DB connect",
        )
        return await list_experiment_slim_tasks(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            limit=limit,
            offset=offset,
            include_empty_rewards=True,
            record_timing=_make_timing_recorder(request),
        )


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
    statuses: str | None = Query(None, description="Task status CSV"),
    priorities: str | None = Query(None, description="Task priority CSV"),
    verdict_statuses: str | None = Query(None, description="Task verdict status CSV"),
    has_link: bool | None = Query(None),
    run_analysis: bool | None = Query(None),
    run_probe: bool | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    trial_finished_after: datetime | None = Query(None),
    trial_finished_before: datetime | None = Query(None),
    experiment_ids: str | None = Query(None, description="Experiment id CSV"),
    agents: str | None = Query(None, description="Trial agent CSV"),
    models: str | None = Query(None, description="Trial model CSV"),
    agent_models: str | None = Query(
        None, description="Agent+model pair CSV, each 'agent:model'"
    ),
    providers: str | None = Query(None, description="Trial provider CSV"),
    environments: str | None = Query(None, description="Trial environment CSV"),
    trial_statuses: str | None = Query(None, description="Trial status CSV"),
    origins: str | None = Query(None, description="Trial origin CSV"),
    trial_is_probe: bool | None = Query(None),
    harbor_shas: str | None = Query(None, description="Harbor SHA CSV"),
    harbor_stages: str | None = Query(None, description="Harbor stage CSV"),
    analysis_classifications: str | None = Query(
        None, description="Trial analysis classification CSV"
    ),
    has_error: bool | None = Query(None),
    has_trajectory: bool | None = Query(None),
    min_attempts: int | None = Query(None, ge=1),
    min_tokens: int | None = Query(None, ge=0),
    max_tokens: int | None = Query(None, ge=0),
    min_steps: int | None = Query(None, ge=0),
    max_steps: int | None = Query(None, ge=0),
    min_duration_seconds: float | None = Query(None, ge=0),
    max_duration_seconds: float | None = Query(None, ge=0),
    min_tool_calls: int | None = Query(None, ge=0),
    max_tool_calls: int | None = Query(None, ge=0),
    tool_names: str | None = Query(None, description="Tool function name CSV"),
    tool_count_mins: str | None = Query(
        None, description="JSON object of tool name to minimum count"
    ),
    trial_metric_match: str = Query("any", pattern="^(any|all)$"),
    reward_min: float | None = Query(None, ge=0.0, le=1.0),
    reward_max: float | None = Query(None, ge=0.0, le=1.0),
    # --- Phase 1.2-lite aggregate filters / sort (computed on the fly) ---
    avg_score_min: float | None = Query(
        None, ge=0.0, le=100.0, description="Task avg score percent (0-100), min"
    ),
    avg_score_max: float | None = Query(
        None, ge=0.0, le=100.0, description="Task avg score percent (0-100), max"
    ),
    total_tokens_min: int | None = Query(None, ge=0),
    total_tokens_max: int | None = Query(None, ge=0),
    total_trials_min: int | None = Query(None, ge=1),
    completed_trials_min: int | None = Query(None, ge=1),
    failed_trials_min: int | None = Query(None, ge=1),
    pass_count_min: int | None = Query(None, ge=1),
    partial_count_min: int | None = Query(None, ge=1),
    fail_count_min: int | None = Query(None, ge=1),
    harness_count_min: int | None = Query(None, ge=1),
    runtime_total_min: float | None = Query(
        None, ge=0.0, description="Task total run time (seconds), min"
    ),
    runtime_total_max: float | None = Query(
        None, ge=0.0, description="Task total run time (seconds), max"
    ),
    runtime_avg_min: float | None = Query(
        None, ge=0.0, description="Task avg run time per trial (seconds), min"
    ),
    runtime_avg_max: float | None = Query(
        None, ge=0.0, description="Task avg run time per trial (seconds), max"
    ),
    pass_rate_min: float | None = Query(
        None, ge=0.0, le=100.0, description="Task pass rate percent (0-100), min"
    ),
    pass_rate_max: float | None = Query(
        None, ge=0.0, le=100.0, description="Task pass rate percent (0-100), max"
    ),
    sort: str | None = Query(
        None,
        description=(
            "Aggregate sort: cost_desc, avg_score_(asc|desc), "
            "total_tokens_(asc|desc), runtime_total_(asc|desc), or "
            "runtime_avg_(asc|desc). Unknown/absent keeps the default recency "
            "order."
        ),
    ),
    # --- Phase 2.1 agent/model comparison (computed on the fly) ---
    compare_by: str | None = Query(
        None, description="Compare subject column: 'agent' or 'model'"
    ),
    compare_a: str | None = Query(None, description="Subject A (agent/model name)"),
    compare_b: str | None = Query(None, description="Subject B (agent/model name)"),
    compare_metric: str | None = Query(
        None,
        description="Compare metric: reward | runtime | tokens | steps | pass_rate",
    ),
    compare_agg: str | None = Query(
        None,
        description=(
            "Reduce each subject's trials by: best | avg | median (default best; "
            "ignored for pass_rate)"
        ),
    ),
    compare_margin: float | None = Query(
        None, ge=0.0, description="A must beat B by more than this (0/absent = any)"
    ),
    compare_margin_unit: str | None = Query(
        None, description="Margin unit: 'pct' (percent of B, default) or 'abs'"
    ),
    top_by: str | None = Query(
        None, description="Top performer subject column: 'agent' or 'model'"
    ),
    top_value: str | None = Query(
        None, description="The subject that must be the task's top performer"
    ),
    top_metric: str | None = Query(
        None,
        description="Top performer metric: reward | runtime | tokens | steps | pass_rate",
    ),
    or_groups: str | None = Query(
        None,
        description=(
            "Phase 2.2 'Match any of…' OR-groups: URL-encoded JSON list of "
            "condition dicts (each dict uses the same field keys as the flat "
            "params). A task matches if it satisfies ANY group; the block is "
            "ANDed with the flat filters."
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
        # Parse the OR-groups JSON defensively: a bad/deep-linked value must not
        # 500 the browse; keep only dict groups, drop the rest.
        parsed_or_groups: list[dict] | None = None
        if or_groups:
            try:
                loaded = json.loads(or_groups)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, list):
                parsed_or_groups = [g for g in loaded if isinstance(g, dict)] or None
        try:
            metric_filter = TrialMetricFilter.from_query(
                models=models,
                min_steps=min_steps,
                max_steps=max_steps,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                min_tool_calls=min_tool_calls,
                max_tool_calls=max_tool_calls,
                tool_names=tool_names,
                tool_count_mins=tool_count_mins,
                match=trial_metric_match,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            statuses=_split_tag_csv(statuses),
            priorities=_split_tag_csv(priorities),
            verdict_statuses=_split_tag_csv(verdict_statuses),
            has_link=has_link,
            run_analysis=run_analysis,
            run_probe=run_probe,
            created_after=created_after,
            created_before=created_before,
            trial_finished_after=trial_finished_after,
            trial_finished_before=trial_finished_before,
            experiment_ids=_split_tag_csv(experiment_ids),
            agents=_split_tag_csv(agents),
            models=metric_filter.models,
            agent_models=_split_tag_csv(agent_models),
            providers=_split_tag_csv(providers),
            environments=_split_tag_csv(environments),
            trial_statuses=_split_tag_csv(trial_statuses),
            origins=_split_tag_csv(origins),
            trial_is_probe=trial_is_probe,
            harbor_shas=_split_tag_csv(harbor_shas),
            harbor_stages=_split_tag_csv(harbor_stages),
            analysis_classifications=_split_tag_csv(analysis_classifications),
            has_error=has_error,
            has_trajectory=has_trajectory,
            min_attempts=min_attempts,
            min_tokens=min_tokens,
            max_tokens=max_tokens,
            min_steps=metric_filter.min_steps,
            max_steps=metric_filter.max_steps,
            min_duration_seconds=metric_filter.min_duration_seconds,
            max_duration_seconds=metric_filter.max_duration_seconds,
            min_tool_calls=metric_filter.min_tool_calls,
            max_tool_calls=metric_filter.max_tool_calls,
            tool_names=metric_filter.tool_names,
            tool_count_mins=metric_filter.tool_count_mins,
            trial_metric_match=metric_filter.match.value,
            reward_min=reward_min,
            reward_max=reward_max,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            total_tokens_min=total_tokens_min,
            total_tokens_max=total_tokens_max,
            total_trials_min=total_trials_min,
            completed_trials_min=completed_trials_min,
            failed_trials_min=failed_trials_min,
            pass_count_min=pass_count_min,
            partial_count_min=partial_count_min,
            fail_count_min=fail_count_min,
            harness_count_min=harness_count_min,
            runtime_total_min=runtime_total_min,
            runtime_total_max=runtime_total_max,
            runtime_avg_min=runtime_avg_min,
            runtime_avg_max=runtime_avg_max,
            pass_rate_min=pass_rate_min,
            pass_rate_max=pass_rate_max,
            sort=sort,
            compare_by=compare_by,
            compare_a=compare_a,
            compare_b=compare_b,
            compare_metric=compare_metric,
            compare_agg=compare_agg,
            compare_margin=compare_margin,
            compare_margin_unit=compare_margin_unit,
            top_by=top_by,
            top_value=top_value,
            top_metric=top_metric,
            or_groups=parsed_or_groups,
            record_timing=_make_timing_recorder(request),
        )


@router.get("/tasks/browse/facets", response_model=TaskBrowseFacets)
async def browse_task_facets(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskBrowseFacets:
    """Distinct filter-option values for the task browser sidebar."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        await session.connection()
        return await browse_task_facets_core(session, org_id=auth.org_id)


@router.get(
    "/tasks/browse/experiment-options", response_model=ExperimentOptionsResponse
)
async def browse_experiment_options(
    auth: Annotated[AuthContext, Depends(require_auth)],
    query: str | None = Query(None),
    ids: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> ExperimentOptionsResponse:
    """Typeahead options for the sidebar experiment filter.

    ``query`` narrows by case-insensitive name substring; ``ids`` (CSV) instead
    hydrates already-selected filter chips and wins over ``query``. Replaces
    the deprecated, always-empty ``facets.experiments`` list.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        await session.connection()
        return await browse_experiment_options_core(
            session,
            org_id=auth.org_id,
            query=query,
            ids=_split_tag_csv(ids),
            limit=limit,
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
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

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


@router.post("/experiments/collections", response_model=TrialCollectionResponse)
async def create_trial_collection(
    payload: TrialCollectionRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TrialCollectionResponse:
    """Gather existing trials into a new read-only collection experiment.

    Trials keep their home experiment; membership is additive via
    ``experiment_trials``. Append-only, so ``tasks`` scope suffices.
    """
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        result = await create_trial_collection_core(
            session,
            name=payload.name,
            trial_ids=payload.trial_ids,
            task_ids=payload.task_ids,
            org_id=auth.org_id,
        )
        await session.commit()

    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.post(
    "/experiments/{experiment_id}/collection/trials",
    response_model=CollectionMutationResponse,
)
async def add_trials_to_collection(
    experiment_id: str,
    payload: CollectionAddRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> CollectionMutationResponse:
    """Link more trials into an existing read-only collection.

    Append-only and idempotent, so ``tasks`` scope suffices -- same reasoning
    as the create route.
    """
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        result = await add_to_collection_core(
            session,
            experiment_id=experiment_id,
            trial_ids=payload.trial_ids,
            task_ids=payload.task_ids,
            from_experiment_ids=payload.from_experiment_ids,
            org_id=auth.org_id,
        )
        await session.commit()

    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.delete(
    "/experiments/{experiment_id}/collection/trials",
    response_model=CollectionMutationResponse,
)
async def remove_trials_from_collection(
    experiment_id: str,
    payload: CollectionRemoveRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CollectionMutationResponse:
    """Drop trials from a collection.

    Requires admin: this changes what an already-published share link
    shows. The trials themselves are untouched.
    """
    async with get_session() as session:
        result = await remove_from_collection_core(
            session,
            experiment_id=experiment_id,
            trial_ids=payload.trial_ids,
            task_ids=payload.task_ids,
            org_id=auth.org_id,
        )
        await session.commit()

    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.patch(
    "/experiments/{experiment_id}/collection",
    response_model=CollectionMutationResponse,
)
async def rename_collection(
    experiment_id: str,
    payload: CollectionRenameRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CollectionMutationResponse:
    """Rename a collection. The share token is unaffected. Requires admin."""
    async with get_session() as session:
        result = await rename_collection_core(
            session,
            experiment_id=experiment_id,
            name=payload.name,
            org_id=auth.org_id,
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

    modal_cancelled = await terminate_run_harvest(result)
    return result | {"modal_calls_cancelled": modal_cancelled}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Soft-delete a task and all of its trials.

    Artifacts remain in storage so the task can be restored. Any active
    workers are cancelled only after the database tombstones commit.
    """
    async with get_session() as session:
        result = await delete_task_core(session, task_id=task_id, org_id=auth.org_id)
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)

    modal_cancelled = await terminate_run_harvest(result)
    return result | {"modal_calls_cancelled": modal_cancelled}


@router.delete("/experiments/{experiment_id}/tasks/{task_id}")
async def unlink_task_from_experiment(
    experiment_id: str,
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Remove a task from one experiment without deleting the task.

    Soft-deletes just the task<->experiment association (the
    ``task_experiments`` join row) plus this experiment's trials for the
    task, so a **shared** task can be pulled out of one experiment while
    staying intact in every other experiment it belongs to. The task row
    itself is never deleted; use ``DELETE /tasks/{task_id}`` for that.
    Artifacts remain in storage (the core path returns an empty
    ``s3_prefixes`` list, so the API layer performs no hard-deletion).
    """
    async with get_session() as session:
        result = await unlink_task_from_experiment_core(
            session,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=auth.org_id,
        )
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)

    modal_cancelled = await terminate_run_harvest(result)
    return result | {"modal_calls_cancelled": modal_cancelled}


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
        experiment.public_token = None
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

    # Post-commit: terminate the harvested FC ids + sandbox targets.
    modal_cancelled = await terminate_run_harvest(result)

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
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        return await rerun_task_qa_core(session, task_id=task_id, org_id=auth.org_id)


@router.post("/tasks/{task_id}/qa/backfill")
async def backfill_task_qa(
    task_id: str,
    body: BackfillQARequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Backfill trial analysis for a task: fill trials with no successful analysis yet.

    Default fills only missing/never-analyzed trials; ``force`` re-runs
    (optionally only ``trial_ids``); ``enable_analysis`` also opts the task
    into analysis going forward.
    """
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        return await backfill_task_analysis_core(
            session,
            task_id=task_id,
            org_id=auth.org_id,
            trial_ids=body.trial_ids,
            force=body.force,
            enable_analysis=body.enable_analysis,
        )


@router.post("/tasks/{task_id}/qa/pre-trial")
async def rerun_pre_trial_audit(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Queue the pre-trial audit for the task's current version.

    Runs only the audit. Does not classify trials and does not synthesize
    the verdict.
    """
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        return await rerun_pre_trial_audit_core(
            session, task_id=task_id, org_id=auth.org_id
        )


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


@router.put(
    "/tasks/{task_id}/versions/{version}/default",
    response_model=TaskVersionResponse,
)
async def set_task_default_version(
    task_id: str,
    version: int,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskVersionResponse:
    """Use a stored task version as the default for display and new runs."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        selected = await set_task_default_version_core(
            session,
            task_id=task_id,
            version=version,
            org_id=auth.org_id,
        )
        await session.commit()

    invalidate_dashboard_cache(org_id=auth.org_id)
    return selected


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
    inline: bool = Query(
        True, description="Include eligible text file contents in the listing"
    ),
    version: int | None = Query(None, description="Task version number"),
    stream: bool = Query(
        False,
        description="Stream NDJSON: the file tree first, then file contents",
    ),
):
    """List all files in a task's S3 directory.

    When presign=True (default), includes presigned URLs for each file,
    allowing clients to fetch content directly from S3 without additional API calls.
    With stream=True the response is NDJSON: a listing chunk as soon as the
    tree is known, then per-file content chunks as they load.
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

    if stream:
        return await make_task_files_ndjson_response(
            stream_task_files_s3(
                task_id=task_id,
                prefix=prefix,
                recursive=recursive,
                limit=limit,
                cursor=cursor,
                presign=presign,
                version=version,
            )
        )

    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=version,
        inline=inline,
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
