from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from oddish.core.endpoints import (
    delete_trial_core,
    get_trial_by_index_core,
    get_task_for_org_core,
    get_trial_for_org_core,
    rerun_trial_analysis_core,
    retry_trial_core,
)
from oddish.core.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.core.trial_imports import (
    complete_trial_import,
    initialize_trial_import,
)
from oddish.core.public_helpers import (
    get_trial_file_content_s3,
    list_task_trials_for_task,
    list_trial_files_s3,
)
from oddish.db.storage import delete_s3_prefixes
from auth import APIKeyScope, AuthContext, require_admin, require_auth
from oddish.db import (
    TrialModel,
    get_session,
)
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


@router.get("/tasks/{task_id}/trials", response_model=list[TrialResponse])
async def list_task_trials(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[TrialResponse]:
    """List all trials for a task (org-scoped)."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        await get_task_for_org_core(session, task_id=task_id, org_id=auth.org_id)

        return await list_task_trials_for_task(session, task_id)


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
) -> dict:
    """Re-queue a failed or completed trial for another attempt."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await retry_trial_core(session, trial_id=trial_id, org_id=auth.org_id)


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
    }


@router.post("/trials/{trial_id}/analysis/retry")
async def retry_trial_analysis(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Queue analysis for a completed trial and invalidate its task verdict."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await rerun_trial_analysis_core(
            session, trial_id=trial_id, org_id=auth.org_id
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


@router.get("/trials/{trial_id}/trajectory")
async def get_trial_trajectory(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict | None:
    """Get ATIF trajectory.json for a trial (step-by-step agent actions)."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_trajectory(trial)


@router.get("/trials/{trial_id}/result")
async def get_trial_result(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get result.json for a trial."""
    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    return await read_trial_result(trial)
