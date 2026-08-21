from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.endpoints import (
    delete_trial_core,
    get_trial_analysis_log_core,
    get_trial_by_index_core,
    get_task_for_org_core,
    get_trial_for_org_core,
    get_trial_response_for_org_core,
    rerun_trial_analysis_core,
    retry_trial_core,
)
from oddish.core.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_probe_artifacts,
    read_trial_result,
    read_trial_trajectory,
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
from oddish.workers.analysis_trials import get_or_create_summarize_trial
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

    The experiment grid opens terminal trial drawers from its slim row. This
    resource remains for direct links, active-analysis polling, and clients
    that explicitly need timing, Harbor, token, or full-analysis fields.
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


@router.post("/trials/{trial_id}/analysis/rerun")
async def rerun_trial_analysis(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Queue analysis for one trial.

    Classifies only this trial. Does not touch other trials, the task
    verdict, or the pre-trial audit.
    """
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)

    async with get_session() as session:
        return await rerun_trial_analysis_core(
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
    version: int | None = Query(
        None,
        description="Scope to trials of one task version; omitted=all versions.",
    ),
) -> list[TrialResponse]:
    """List all trials for a task (org-scoped)."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)

        return await list_task_trials_for_task(
            session, task_id, probe=probe, version=version
        )


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
        owner_user_id=auth.user_id,
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
    include_summary: bool = Query(False),
) -> dict | None:
    """Get ATIF trajectory.json and, when requested, its summary resource."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    trajectory = await read_trial_trajectory(trial)
    if include_summary:
        return {
            "trajectory": trajectory,
            "summary_resource": await _summary_resource_for_trial(trial)
            or {"summary": None, "refresh": None},
        }
    return trajectory


# Trial statuses the summary poller understands, mapped from TrialStatus
# values. The client (`use-trajectory-summary.ts`) polls on exactly these.
_SUMMARY_PENDING_STATUS = {
    "pending": "queued",
    "queued": "queued",
    "running": "running",
    "retrying": "retrying",
    "success": "settling",
}


def _summary_refresh_payload(
    summarize_trial: TrialModel, summary: dict | None
) -> tuple[int, dict]:
    """Build published data and its replacement lifecycle without conflating them."""
    status = summarize_trial.status.value
    if status in {"failed", "skipped"} or summarize_trial.harbor_stage == "cancelled":
        return (
            200 if summary is not None else 409,
            {
                "summary": summary,
                "refresh": {
                    "status": "failed",
                    "job_id": summarize_trial.id,
                    "detail": (
                        "Trajectory summary refresh failed; start a new refresh to retry"
                    ),
                },
            },
        )
    if status in _SUMMARY_PENDING_STATUS:
        return (
            200 if summary is not None else 202,
            {
                "summary": summary,
                "refresh": {
                    "status": _SUMMARY_PENDING_STATUS[status],
                    "job_id": summarize_trial.id,
                    "retry_after_ms": 3000,
                },
            },
        )
    raise RuntimeError(
        f"summary refresh trial {summarize_trial.id} has unsupported status {status}"
    )


def _summary_refresh_response(
    summarize_trial: TrialModel, summary: dict | None
) -> JSONResponse:
    status_code, content = _summary_refresh_payload(summarize_trial, summary)
    return JSONResponse(status_code=status_code, content=content)


async def _summary_resource_for_trial(trial: TrialModel) -> dict | None:
    """Read the published summary and current replacement lifecycle."""
    if trial.trajectory_summary_refresh_trial_id:
        async with get_session() as session:
            refresh_trial = await session.get(
                TrialModel, trial.trajectory_summary_refresh_trial_id
            )
        if (
            refresh_trial is None
            or refresh_trial.kind != "summarize"
            or refresh_trial.task_id != trial.task_id
        ):
            raise RuntimeError(
                f"trial {trial.id} points to invalid summary refresh "
                f"{trial.trajectory_summary_refresh_trial_id}"
            )
        return _summary_refresh_payload(refresh_trial, trial.trajectory_summary)[1]
    if trial.trajectory_summary is not None:
        return {"summary": trial.trajectory_summary, "refresh": None}
    return None


@router.get("/trials/{trial_id}/trajectory/summary")
async def get_trial_trajectory_summary(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Read the published summary or report the current refresh lifecycle."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    resource = await _summary_resource_for_trial(trial)
    if resource is not None:
        refresh = resource["refresh"]
        if refresh is not None:
            status_code = (
                409
                if refresh["status"] == "failed" and resource["summary"] is None
                else 202
                if refresh["status"] != "failed" and resource["summary"] is None
                else 200
            )
            return JSONResponse(status_code=status_code, content=resource)
        return resource
    raise HTTPException(status_code=404, detail="No trajectory summary for this trial")


@router.post("/trials/{trial_id}/trajectory/summary")
async def regenerate_trial_trajectory_summary(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Start or adopt the paid summarize trial for one agent trajectory."""
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    trial = await _get_authorized_trial(trial_id, auth)
    async with get_session() as session:
        refresh_trial = await get_or_create_summarize_trial(
            session, target_trial_id=trial.id
        )
    if refresh_trial is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This trial cannot be summarized: only agent trials with "
                "a recorded trajectory are eligible"
            ),
        )
    return _summary_refresh_response(refresh_trial, trial.trajectory_summary)


@router.get("/trials/{trial_id}/result")
async def get_trial_result(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get result.json for a trial."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_result(trial)
