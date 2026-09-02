"""Public (unauthenticated) routes for shared experiments, tasks, and trials."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from oddish.core.endpoints.experiment_page import (
    get_public_experiment_focus_core,
    get_public_experiment_open_core,
    get_public_experiment_trial_page_core,
)
from oddish.core.endpoints.experiment_cost import get_experiment_cost_totals
from oddish.core.helpers import build_task_status_response, fetch_trial_queue_info
from oddish.core.model_display_names import (
    apply_model_display_names,
    experiment_display_names,
    mask_trajectory_model_names,
)
from oddish.core.sharing.public_projection import public_task_github_meta
from oddish.core.tags.projection import list_effective_user_tags_for_task_versions
from oddish.core.trial_live import read_trial_live
from oddish.core.task_files import resolve_task_file_source
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
    make_task_files_ndjson_response,
    stream_task_files_s3,
)
from oddish.db import (
    ExperimentModel,
    TrialModel,
    get_session,
    task_experiments,
)
from oddish.schemas import (
    ExperimentCostTotals,
    ExperimentTrialPageResponse,
    PublicExperimentFocusResponse,
    PublicExperimentListItem,
    PublicExperimentOpenResponse,
    PublicExperimentResponse,
    PublicTaskStatusResponse,
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


async def _detached_public_trial_with_display_names(
    public_token: str, trial_id: str
) -> tuple[TrialModel, dict[str, str]]:
    """A public trial plus this experiment's alias table, both read before I/O.

    One session for the pair, released before the S3 read for the same reason
    :func:`_get_detached_public_trial` releases it. The experiment is loaded
    once here and handed to the trial lookup so it isn't re-queried.
    """
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if not experiment:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        trial = await get_public_trial_for_experiment(
            session, public_token, trial_id, experiment=experiment
        )
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        session.expunge(trial)
        return trial, experiment_display_names(experiment)


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


@router.get(
    "/public/experiments/{public_token}/cost-totals",
    response_model=ExperimentCostTotals,
)
async def get_public_experiment_cost_totals(
    public_token: str,
) -> ExperimentCostTotals:
    async with get_session() as session:
        experiment = await get_public_experiment(session, public_token)
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")
        return await get_experiment_cost_totals(
            session,
            experiment_id=experiment.id,
            org_id=experiment.org_id,
        )


@router.get(
    "/public/experiments/{public_token}/open",
    response_model=PublicExperimentOpenResponse,
)
async def get_public_experiment_open(
    public_token: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    before_created_at: datetime | None = None,
    before_task_id: str | None = None,
    include_summary: bool = True,
) -> PublicExperimentOpenResponse:
    async with get_session() as session:
        return await get_public_experiment_open_core(
            session,
            public_token=public_token,
            limit=limit,
            before_created_at=before_created_at,
            before_task_id=before_task_id,
            include_summary=include_summary,
        )


@router.get(
    "/public/experiments/{public_token}/focus",
    response_model=PublicExperimentFocusResponse,
)
async def get_public_experiment_focus(
    public_token: str,
    task: str | None = None,
    trial: str | None = None,
) -> PublicExperimentFocusResponse:
    async with get_session() as session:
        return await get_public_experiment_focus_core(
            session,
            public_token=public_token,
            task_selector=task,
            trial_id=trial,
        )


@router.get(
    "/public/experiments/{public_token}/trial-page",
    response_model=ExperimentTrialPageResponse,
)
async def get_public_experiment_trial_page(
    public_token: str,
    limit: Annotated[int, Query(ge=1, le=250)] = 250,
    before_created_at: datetime | None = None,
    before_trial_id: str | None = None,
) -> ExperimentTrialPageResponse:
    async with get_session() as session:
        return await get_public_experiment_trial_page_core(
            session,
            public_token=public_token,
            limit=limit,
            before_created_at=before_created_at,
            before_trial_id=before_trial_id,
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
    response: TaskStatusResponse | PublicTaskStatusResponse,
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
    "/public/experiments/{public_token}/tasks/{task_id}",
    response_model=PublicTaskStatusResponse,
)
async def get_public_task_status(
    public_token: str,
    task_id: str,
    include_trials: bool = True,
) -> PublicTaskStatusResponse:
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
        apply_model_display_names(response.trials or [], experiment_display_names(exp))
        public_response = PublicTaskStatusResponse.model_validate(response)
        public_response.github_meta = public_task_github_meta(response.github_meta)
        return public_response


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


@router.get("/public/experiments/{public_token}/trials/{trial_id}/live")
async def get_public_trial_live(
    public_token: str,
    trial_id: str,
    attempt: int | None = Query(None),
    after_seq: int = Query(0),
) -> dict:
    async with get_session() as session:
        trial = await get_public_trial_for_experiment(session, public_token, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        return await read_trial_live(
            session, trial, attempt=attempt, after_seq=after_seq
        )


@router.get("/public/experiments/{public_token}/trials/{trial_id}/logs/structured")
async def get_public_trial_logs_structured(public_token: str, trial_id: str) -> dict:
    """Get structured logs for a public trial."""
    trial = await _get_detached_public_trial(public_token, trial_id)
    return await read_trial_logs_structured(trial)


@router.get("/public/experiments/{public_token}/trials/{trial_id}/trajectory")
async def get_public_trial_trajectory(public_token: str, trial_id: str) -> dict | None:
    """Get ATIF trajectory.json for a public trial.

    Masked: the step headers render the trajectory's own ``model_name``, so an
    unmasked payload prints the real id right under the aliased one the trial
    grid shows.

    This closes one hole, not the class. An alias is a display convenience, NOT
    a boundary: the same drawer's Files tab serves ``config.json`` -- which
    ``ConfigJsonRenderer`` prints under a literal "Model" label -- and
    ``agent/trajectory.json``, the very bytes this route rewrites, both with
    the real id. Masking those means rewriting a run's recorded output, which
    is a bigger call than this route. Don't read the admin UI's promise as
    airtight until they're covered.
    """
    trial, names = await _detached_public_trial_with_display_names(
        public_token, trial_id
    )
    return mask_trajectory_model_names(await read_trial_trajectory(trial), names)


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
    stream: bool = Query(
        False,
        description="Stream NDJSON: the file tree first, then file contents",
    ),
):
    """List all files in a public task's S3 directory."""
    async with get_session() as session:
        resolved = await get_public_task_for_experiment(session, public_token, task_id)
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        version, task_s3_prefix = await resolve_task_file_source(
            session, task_id=task_id, version=version
        )

    if stream:
        return await make_task_files_ndjson_response(
            stream_task_files_s3(
                task_id=task_id,
                prefix=prefix,
                recursive=recursive,
                limit=limit,
                cursor=cursor,
                presign=presign,
                task_s3_prefix=task_s3_prefix,
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
        task_s3_prefix=task_s3_prefix,
        version=version,
    )


@router.get("/public/experiments/{public_token}/tasks/{task_id}/files/{file_path:path}")
async def get_public_task_file_content(
    public_token: str,
    task_id: str,
    file_path: str,
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
    max_bytes: int | None = Query(None, ge=1),
) -> dict:
    """Get content of a specific public task file from S3."""
    async with get_session() as session:
        resolved = await get_public_task_for_experiment(session, public_token, task_id)
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        version, task_s3_prefix = await resolve_task_file_source(
            session, task_id=task_id, version=version
        )

    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        task_s3_prefix=task_s3_prefix,
        version=version,
        max_bytes=max_bytes,
    )
