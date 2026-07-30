from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.endpoints import (
    delete_trial_core,
    get_trial_analysis_log_core,
    get_trial_by_index_core,
    get_task_for_org_core,
    get_trial_for_org_core,
    get_trial_response_for_org_core,
    retry_trial_core,
)
from oddish.core.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_probe_artifacts,
    read_trial_result,
    read_trial_trajectory,
    read_persisted_trajectory_graph,
    generate_and_store_trajectory_graph,
)
from oddish.core.trial_live import read_trial_live_for_id
from oddish.core.ingest.trial_imports import (
    complete_trial_import,
    initialize_trial_import,
)
from oddish.core.sharing.helpers import (
    get_trial_file_content_s3,
    list_experiment_trials_for_org,
    list_task_trials_for_task,
    list_trial_files_s3,
)
from oddish.db.storage import delete_s3_prefixes
from auth import APIKeyScope, AuthContext, require_admin, require_auth
from oddish.db import (
    TrialModel,
    get_session,
)
from oddish.schemas import TrialRetryRequest
from oddish.schemas import (
    TrialImportCompleteRequest,
    TrialImportCompleteResponse,
    TrialImportInitRequest,
    TrialImportInitResponse,
    TrialResponse,
)

import logging

from api.services.summarize_trajectory import (
    SummaryGenerationError,
    get_or_generate_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Trials"])


async def _get_authorized_trial(trial_id: str, auth: AuthContext) -> TrialModel:
    """Load a trial, then release the DB session before artifact I/O."""
    async with get_session() as session:
        trial = await get_trial_for_org_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )
        session.expunge(trial)
        return trial


@router.get("/tasks/{task_id}/trials/{index}", response_model=TrialResponse)
async def get_trial(
    task_id: str,
    index: int,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TrialResponse:
    """Get a specific trial by its 0-based index within the task."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_by_index_core(
            session, task_id=task_id, index=index, org_id=auth.org_id
        )


@router.get("/trials/{trial_id}", response_model=TrialResponse)
async def get_trial_full(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TrialResponse:
    """Full detail for a single trial by id.

    The experiment grid loads only slim trials; clicking a cell fetches the
    full trial here (timing, harbor, tokens, full analysis, etc.).
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_response_for_org_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )


@router.get("/trials/{trial_id}/analysis-log")
async def get_trial_analysis_log(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Whole log of the trial's current/most recent analysis run, plus the
    QA queue position while the job waits for a worker."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_analysis_log_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )


@router.get("/tasks/{task_id}/trials", response_model=list[TrialResponse])
async def list_task_trials(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    probe: bool | None = Query(
        None,
        description="Filter by trial kind: true=probes only, false=real attempts only, omitted=all.",
    ),
) -> list[TrialResponse]:
    """List all trials for a task (org-scoped)."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)

        return await list_task_trials_for_task(session, task_id, probe=probe)


@router.get("/experiments/{experiment_id}/trials", response_model=list[TrialResponse])
async def list_experiment_trials(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[TrialResponse]:
    """List all non-superseded trials for an experiment (org-scoped)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_experiment_trials_for_org(session, experiment_id, auth.org_id)


# =============================================================================
# Trial Import (off-oddish Harbor runs)
# =============================================================================


@router.post("/trials/import/init", response_model=TrialImportInitResponse)
async def init_trial_import(
    payload: TrialImportInitRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TrialImportInitResponse:
    """Register an off-oddish trial and return a presigned artifact URL."""
    auth.require_scope(APIKeyScope.TASKS)
    return await initialize_trial_import(
        task_id=payload.task_id,
        experiment_id_or_name=payload.experiment_id,
        trial_spec=payload.trial,
        upload_artifacts=payload.upload_artifacts,
        org_id=auth.org_id,
    )


@router.post("/trials/import/complete", response_model=TrialImportCompleteResponse)
async def finalize_trial_import(
    payload: TrialImportCompleteRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TrialImportCompleteResponse:
    """Finalize an imported trial after the client PUTs its archive."""
    auth.require_scope(APIKeyScope.TASKS)
    return await complete_trial_import(
        trial_id=payload.trial_id,
        org_id=auth.org_id,
    )


@router.post("/trials/{trial_id}/retry")
async def retry_trial(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    payload: TrialRetryRequest | None = Body(default=None),
) -> dict:
    """Re-queue a failed or completed trial for another attempt."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        result = await retry_trial_core(
            session,
            trial_id=trial_id,
            org_id=auth.org_id,
            registry_auth=(payload.registry_auth if payload else None),
            gate_baselines=(payload.gate_baselines if payload else True),
        )

    from oddish.core.helpers import terminate_run_harvest

    modal_cancelled = await terminate_run_harvest(result)
    return result | {"modal_calls_cancelled": modal_cancelled}


@router.delete("/trials/{trial_id}")
async def delete_trial(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Delete a single trial (DB row + S3 artifacts).

    Admin-only. Cancels in-flight worker_jobs for the trial and
    invalidates the parent task's cached verdict so dashboards stop
    reflecting the deleted row.
    """
    async with get_session() as session:
        result = await delete_trial_core(session, trial_id=trial_id, org_id=auth.org_id)
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)

    from oddish.core.helpers import terminate_run_harvest

    modal_cancelled = await terminate_run_harvest(result)

    s3_prefixes = result.get("s3_prefixes", []) or []
    s3_keys_deleted = 0
    if s3_prefixes:
        try:
            s3_keys_deleted = await delete_s3_prefixes(s3_prefixes)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning(
                "Trial %s row deleted, but S3 cleanup failed: %s",
                trial_id,
                exc,
            )

    return {
        "deleted": result.get("deleted", {"trial_id": trial_id}),
        "s3_prefixes": s3_prefixes,
        "s3_keys_deleted": s3_keys_deleted,
        "modal_calls_cancelled": modal_cancelled,
    }


@router.get("/trials/{trial_id}/live")
async def get_trial_live(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    attempt: int | None = Query(None),
    after_seq: int = Query(0),
) -> dict:
    """Live transcript events + running usage for a trial ((attempt, seq) cursor)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await read_trial_live_for_id(
            session,
            trial_id=trial_id,
            org_id=auth.org_id,
            attempt=attempt,
            after_seq=after_seq,
        )


@router.get("/trials/{trial_id}/logs")
async def get_trial_logs(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get logs for a specific trial."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_logs(trial)


@router.get("/trials/{trial_id}/logs/structured")
async def get_trial_logs_structured(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get logs for a trial, structured by category (agent, verifier, exception)."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_logs_structured(trial)


@router.get("/trials/{trial_id}/files")
async def list_trial_files(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
) -> dict:
    """List all files in S3 for a trial, with presigned URLs for direct access."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await list_trial_files_s3(
        trial,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
    )


@router.get("/trials/{trial_id}/debug-files")
async def debug_trial_files_endpoint(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Debug endpoint: list all files in S3 for a trial."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)

    from oddish.core.trial_io import debug_trial_files

    return await debug_trial_files(trial)


@router.get("/trials/{trial_id}/files/{file_path:path}")
async def get_trial_file(
    trial_id: str,
    file_path: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    """Get a file from a trial's S3 directory by relative path.

    Tries the general S3 path first (any file in the trial directory),
    then falls back to the agent/ subdirectory for backward compatibility.
    """
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    try:
        content, media_type = await get_trial_file_content_s3(trial, file_path)
        return Response(content=content, media_type=media_type)
    except HTTPException:
        pass
    content, media_type = await read_trial_agent_file(trial, file_path)
    return Response(content=content, media_type=media_type)


@router.get("/trials/{trial_id}/probe-artifacts")
async def get_trial_probe_artifacts(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get the probe `_artifacts` blob (agent transcript, verifier stdout,
    trajectory, watchdog log) for a trial.

    Cloud trials never inline this into ``trial.result``; it's read on demand
    from object storage so the probe result page can render the agent output.
    """
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_probe_artifacts(trial)


@router.get("/trials/{trial_id}/trajectory")
async def get_trial_trajectory(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict | None:
    """Get ATIF trajectory.json for a trial (step-by-step agent actions)."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_trajectory(trial)


@router.get("/trials/{trial_id}/trajectory/graph")
async def get_trial_trajectory_graph(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Return the STORED agent graph for a trial, or ``{"status":
    "not_generated"}`` when none exists yet.

    Read-only and free — never runs the LLM. Generation is an explicit POST to
    the same path (the "Generate agent graph" / "Rebuild" button).
    """
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    graph = read_persisted_trajectory_graph(trial)
    if graph is None:
        return {"status": "not_generated"}
    return graph


@router.post("/trials/{trial_id}/trajectory/graph")
async def generate_trial_trajectory_graph(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    refresh: bool = False,
) -> dict:
    """Generate (and persist) the condensed agent step-graph for a trial.

    Distills the full ATIF trajectory into a handful of general phases plus a
    terminal node (last action + why the run ended), reusing the trajectory
    summary's phase segmentation when available so Agent Graph and Summary stay
    consistent. ``?refresh=true`` regenerates even if one is already stored.
    """
    auth.require_scope(APIKeyScope.TASKS)
    from oddish.core.helpers import _has_fetchable_trajectory

    trial = await _get_authorized_trial(trial_id, auth)

    async with get_session() as session:
        attached_trial = await session.get(TrialModel, trial.id)
        if attached_trial is None:
            raise HTTPException(status_code=404, detail="Trial not found")

        # Mirror the trajectory endpoint's notion of "has a trajectory" — true
        # for finished Grok Build runs (grok-build.json synthesized to ATIF),
        # not just rows with the has_trajectory column set.
        if not _has_fetchable_trajectory(attached_trial):
            raise HTTPException(
                status_code=404, detail="No trajectory available for this trial"
            )

        # Best-effort: reuse the shipped trajectory-summary phases. If the
        # summary can't be produced (no trajectory, LLM error), fall through to
        # the graph's own segmentation rather than failing the request.
        summary: dict | None = None
        try:
            summary = await get_or_generate_summary(
                session, attached_trial, triggered_by_user_id=auth.user_id
            )
        except Exception as e:
            # Best-effort: the graph reuses the summary's phases when present but
            # segments the run itself otherwise. Any failure here (generation
            # error, a DB error inside the summary path) must not abort the graph.
            logger.warning(
                "Trajectory summary unavailable for graph %s: %s", trial_id, e
            )

        return await generate_and_store_trajectory_graph(
            session, attached_trial, refresh=refresh, summary=summary
        )


@router.get("/trials/{trial_id}/trajectory/summary")
async def get_trial_trajectory_summary(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get a Claude-generated summary of the trajectory.

    Returns the summary from the latest `analyzer_blocks` row (mirrored to
    `trials.trajectory_summary`) when fresh, otherwise generates one. 404 when
    the trial has no trajectory; 502 if generation fails.
    """
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    try:
        async with get_session() as session:
            attached_trial = await session.get(TrialModel, trial.id)
            if attached_trial is None:
                raise HTTPException(status_code=404, detail="Trial not found")
            summary = await get_or_generate_summary(
                session, attached_trial, triggered_by_user_id=auth.user_id
            )
    except SummaryGenerationError as e:
        logger.error(
            "Trajectory summary generation failed for trial %s: %s", trial_id, e
        )
        raise HTTPException(
            status_code=502, detail=f"Summary generation failed: {e}"
        )
    if summary is None:
        raise HTTPException(
            status_code=404, detail="No trajectory available for this trial"
        )
    return summary


@router.get("/trials/{trial_id}/result")
async def get_trial_result(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get result.json for a trial."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_result(trial)
