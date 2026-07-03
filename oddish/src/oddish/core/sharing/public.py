"""Public (unauthenticated) routes for shared experiments, tasks, and trials."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from oddish.core.experiment_membership import gathered_trial_ids_select
from oddish.core.helpers import build_task_status_response, fetch_trial_queue_info
from oddish.core.tags.projection import list_effective_user_tags_for_task_versions
from oddish.core.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from .helpers import (
    get_public_experiment,
    get_public_task_for_experiment,
    get_public_trial_for_experiment,
    get_task_file_content_s3,
    get_trial_file_content_s3,
    list_task_trials_for_public_experiment,
    list_task_files_s3,
    list_trial_files_s3,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    task_experiments,
)
from oddish.schemas import (
    PublicExperimentListItem,
    PublicExperimentResponse,
    TaskBrowseExperiment,
    TaskStatusResponse,
    TrialResponse,
    UserTagRef,
)

router = APIRouter(tags=["Public"])


async def _hydrate_public_user_tags(session, *, task_ids: list[str]) -> dict:
    """Return the same UserTagView shape as the authenticated path but
    filtered to ``tags.visibility = 'PUBLIC'``.

    Public endpoints (``/share/*``, ``/datasets/*``) call this when
    serializing a task DTO. PRIVATE tags simply don't appear.
    """
    return await list_effective_user_tags_for_task_versions(
        session, task_ids=list(task_ids), public_only=True
    )


def _user_tag_refs(views) -> list[UserTagRef]:
    """Map ``UserTagView`` rows from the resolver to ``UserTagRef`` DTOs."""
    return [
        UserTagRef(
            tag_id=t.tag_id,
            key=t.key,
            value=t.value,
            color=t.color,
            visibility=t.visibility,
            current=t.current,
            older=t.older,
        )
        for t in views
    ]


async def _get_detached_public_trial(public_token: str, trial_id: str) -> TrialModel:
    """Load a public trial, then release the DB session before artifact I/O."""
    async with get_session() as session:
        trial = await get_public_trial_for_experiment(session, public_token, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        session.expunge(trial)
        return trial


@router.get(
    "/public/experiments",
    response_model=list[PublicExperimentListItem],
)
async def list_public_experiments(
    limit: int = 100,
    offset: int = 0,
) -> list[PublicExperimentListItem]:
    """Do not enumerate public share links.

    Direct ``/public/experiments/{public_token}`` lookups remain available for
    users who already have a link, but the unauthenticated list endpoint must
    not disclose share tokens.
    """
    _ = (limit, offset)
    return []


@router.get(
    "/public/experiments/{public_token}", response_model=PublicExperimentResponse
)
async def get_public_experiment_info(public_token: str) -> PublicExperimentResponse:
    """Get public experiment metadata by share token."""
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return PublicExperimentResponse(
            name=experiment.name,
            public_token=experiment.public_token or public_token,
            description=experiment.description,
        )


async def _public_experiment_refs(
    session, task_ids: list[str]
) -> dict[str, list[tuple[str, str, datetime | None]]]:
    """(id, name, created_at) of PUBLIC experiments per task.

    The shared response builder fills ``experiments`` and the singular
    ``experiment_*`` fields from every live membership, which is correct
    for org-authenticated callers but would leak private experiment
    names/ids on the anonymous public endpoints -- public responses get
    both replaced via :func:`_apply_public_experiments`.
    """
    if not task_ids:
        return {}
    rows = await session.execute(
        select(
            task_experiments.c.task_id,
            ExperimentModel.id,
            ExperimentModel.name,
            ExperimentModel.created_at,
        )
        .select_from(task_experiments)
        .join(
            ExperimentModel,
            ExperimentModel.id == task_experiments.c.experiment_id,
        )
        .where(
            task_experiments.c.task_id.in_(task_ids),
            task_experiments.c.deleted_at.is_(None),
            ExperimentModel.is_public == True,  # noqa: E712
        )
        .order_by(ExperimentModel.name.asc(), ExperimentModel.id.asc())
    )
    refs: dict[str, list[tuple[str, str, datetime | None]]] = {}
    for task_id, experiment_id, experiment_name, experiment_created_at in rows.all():
        refs.setdefault(str(task_id), []).append(
            (str(experiment_id), str(experiment_name), experiment_created_at)
        )
    return refs


def _apply_public_experiments(
    response: TaskStatusResponse,
    refs: list[tuple[str, str, datetime | None]],
    *,
    preferred_id: str | None = None,
) -> None:
    """Replace BOTH the experiments list and the singular experiment_*
    fields with the public-only projection (the builder derives them from
    all memberships, including private ones)."""
    response.experiments = [
        TaskBrowseExperiment(id=ref_id, name=ref_name) for ref_id, ref_name, _ in refs
    ]
    primary = None
    if preferred_id is not None:
        primary = next((r for r in refs if r[0] == preferred_id), None)
    if primary is None:
        primary = refs[0] if refs else None
    response.experiment_id = primary[0] if primary else ""
    response.experiment_name = primary[1] if primary else ""
    response.experiment_is_public = primary is not None
    response.experiment_created_at = primary[2] if primary else None


@router.get(
    "/public/experiments/{public_token}/tasks", response_model=list[TaskStatusResponse]
)
async def list_public_experiment_tasks(
    public_token: str,
    limit: int = 200,
    offset: int = 0,
) -> list[TaskStatusResponse]:
    """List tasks (with trials) for a public experiment."""
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        query = (
            select(TaskModel)
            .options(
                selectinload(TaskModel.trials),
                selectinload(TaskModel.experiments),
            )
            .where(
                TaskModel.experiments.any(
                    and_(
                        ExperimentModel.public_token == public_token,
                        ExperimentModel.is_public == True,  # noqa: E712
                    )
                )
            )
            .order_by(TaskModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await session.execute(query)
        tasks = result.scalars().all()

        exp_id_result = await session.execute(
            select(ExperimentModel.id).where(
                ExperimentModel.public_token == public_token,
                ExperimentModel.is_public == True,  # noqa: E712
            )
        )
        exp_id = exp_id_result.scalar_one_or_none()
        from sqlalchemy.orm.attributes import set_committed_value

        gathered_ids: set[str] = set()
        if exp_id:
            gathered_ids = set(
                (await session.execute(gathered_trial_ids_select(exp_id)))
                .scalars()
                .all()
            )

        for task in tasks:
            # Scope to this experiment's trials (home or gathered) and never
            # expose probes — probes are experimental and stay out of the
            # public share view, gathered or not.
            filtered = [
                t
                for t in task.trials
                if not t.is_probe
                and (not exp_id or t.experiment_id == exp_id or t.id in gathered_ids)
            ]
            set_committed_value(task, "trials", filtered)

        queue_info_by_trial_id = await fetch_trial_queue_info(
            session,
            trials=[trial for task in tasks for trial in task.trials],
        )
        user_tags_by_task = await _hydrate_public_user_tags(
            session, task_ids=[task.id for task in tasks]
        )
        responses = [
            build_task_status_response(
                task,
                queue_info_by_trial_id=queue_info_by_trial_id,
                experiment_context_id=exp_id,
                gathered_trial_ids=gathered_ids,
            )
            for task in tasks
        ]
        public_exps = await _public_experiment_refs(
            session, [task.id for task in tasks]
        )
        for resp, task in zip(responses, tasks):
            resp.user_tags = _user_tag_refs(user_tags_by_task.get(task.id, []))
            _apply_public_experiments(
                resp, public_exps.get(task.id, []), preferred_id=exp_id
            )
        return responses


@router.get(
    "/public/experiments/{public_token}/tasks/{task_id}",
    response_model=TaskStatusResponse,
)
async def get_public_task_status(
    public_token: str,
    task_id: str,
    include_trials: bool = True,
) -> TaskStatusResponse:
    """Get task status for a public experiment."""
    async with get_session() as session:
        resolved = await get_public_task_for_experiment(session, public_token, task_id)
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        exp, task, gathered_ids = resolved
        queue_info_by_trial_id = await fetch_trial_queue_info(
            session,
            trials=task.trials if include_trials else [],
        )
        response = build_task_status_response(
            task,
            include_trials=include_trials,
            queue_info_by_trial_id=queue_info_by_trial_id,
            experiment_context_id=exp.id,
            gathered_trial_ids=gathered_ids,
        )
        user_tags_by_task = await _hydrate_public_user_tags(session, task_ids=[task.id])
        response.user_tags = _user_tag_refs(user_tags_by_task.get(task.id, []))
        public_exps = await _public_experiment_refs(session, [task.id])
        _apply_public_experiments(
            response, public_exps.get(task.id, []), preferred_id=exp.id
        )
        return response


@router.get(
    "/public/experiments/{public_token}/tasks/{task_id}/trials",
    response_model=list[TrialResponse],
)
async def list_public_task_trials(
    public_token: str, task_id: str
) -> list[TrialResponse]:
    """List real-attempt trials for a public task.

    Probes are experimental and never exposed publicly, so this always
    filters to real attempts (``probe=False``) regardless of caller input.
    """
    async with get_session() as session:
        trials = await list_task_trials_for_public_experiment(
            session, public_token, task_id
        )
        if trials is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return trials


@router.get("/public/experiments/{public_token}/trials/{trial_id}/logs")
async def get_public_trial_logs(public_token: str, trial_id: str) -> dict:
    """Get logs for a public trial."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await read_trial_logs(trial)


@router.get("/public/experiments/{public_token}/trials/{trial_id}/logs/structured")
async def get_public_trial_logs_structured(public_token: str, trial_id: str) -> dict:
    """Get structured logs for a public trial."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await read_trial_logs_structured(trial)


@router.get("/public/experiments/{public_token}/trials/{trial_id}/trajectory")
async def get_public_trial_trajectory(public_token: str, trial_id: str) -> dict | None:
    """Get ATIF trajectory.json for a public trial."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await read_trial_trajectory(trial)


@router.get("/public/experiments/{public_token}/trials/{trial_id}/files")
async def list_public_trial_files(
    public_token: str,
    trial_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
) -> dict:
    """List all files in a public trial's S3 directory."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await list_trial_files_s3(
        trial,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
    )


@router.get(
    "/public/experiments/{public_token}/trials/{trial_id}/files/{file_path:path}"
)
async def get_public_trial_file(
    public_token: str, trial_id: str, file_path: str
) -> Response:
    """Get a file from a public trial's S3 directory."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    try:
        content, media_type = await get_trial_file_content_s3(trial, file_path)
        return Response(content=content, media_type=media_type)
    except HTTPException:
        pass
    content, media_type = await read_trial_agent_file(trial, file_path)
    return Response(content=content, media_type=media_type)


@router.get("/public/experiments/{public_token}/trials/{trial_id}/result")
async def get_public_trial_result(public_token: str, trial_id: str) -> dict:
    """Get result.json for a public trial."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await read_trial_result(trial)


@router.get("/public/experiments/{public_token}/tasks/{task_id}/files")
async def list_public_task_files(
    public_token: str,
    task_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """List all files in a public task's S3 directory."""
    async with get_session() as session:
        resolved = await get_public_task_for_experiment(
            session, public_token, task_id, load_current_version=True
        )
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        _, task, _ = resolved
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


@router.get("/public/experiments/{public_token}/tasks/{task_id}/files/{file_path:path}")
async def get_public_task_file_content(
    public_token: str,
    task_id: str,
    file_path: str,
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    """Get content of a specific public task file from S3."""
    async with get_session() as session:
        resolved = await get_public_task_for_experiment(
            session, public_token, task_id, load_current_version=True
        )
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        _, task, _ = resolved
        if version is None and task.current_version:
            version = task.current_version.version

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=version,
    )
