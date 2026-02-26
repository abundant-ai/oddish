from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from oddish.api.endpoints import (
    get_trial_by_index_core,
    get_task_for_org_core,
    get_trial_for_org_core,
    get_trial_logs_core,
    get_trial_logs_structured_core,
    retry_trial_core,
    get_trial_result_core,
    get_trial_trajectory_core,
)
from oddish.api.trial_io import read_trial_agent_file
from api.routers._helpers import (
    get_trial_file_content_s3,
    list_task_trials_for_task,
    list_trial_files_s3,
)
from auth import APIKeyScope, AuthContext, require_auth
from oddish.config import settings
from oddish.db import (
    get_session,
    get_storage_client,
)
from oddish.db.storage import StorageClient
from oddish.schemas import TrialResponse

router = APIRouter(tags=["Trials"])


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


@router.post("/trials/{trial_id}/retry")
async def retry_trial(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Re-queue a failed or completed trial for another attempt."""
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        return await retry_trial_core(session, trial_id=trial_id, org_id=auth.org_id)


@router.get("/trials/{trial_id}/logs")
async def get_trial_logs(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get logs for a specific trial."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_logs_core(session, trial_id=trial_id, org_id=auth.org_id)


@router.get("/trials/{trial_id}/logs/structured")
async def get_trial_logs_structured(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get logs for a trial, structured by category (agent, verifier, exception)."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_logs_structured_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )


@router.get("/trials/{trial_id}/files")
async def list_trial_files(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """List all files in S3 for a trial, with presigned URLs for direct access."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        trial = await get_trial_for_org_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )
        return await list_trial_files_s3(trial)


@router.get("/trials/{trial_id}/debug-files")
async def debug_trial_files(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Debug endpoint: list all files in S3 for a trial."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        trial = await get_trial_for_org_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )

        result = {
            "trial_id": trial_id,
            "trial_s3_key": trial.trial_s3_key,
            "computed_prefix": StorageClient._trial_prefix(trial_id),
            "harbor_result_path": trial.harbor_result_path,
            "s3_enabled": settings.s3_enabled,
            "files": [],
            "trajectory_files": [],
            "error": None,
        }

        if not settings.s3_enabled:
            result["error"] = "S3 not enabled"
            return result

        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial_id)
        result["using_prefix"] = s3_prefix

        storage = get_storage_client()
        try:
            # List all files under this prefix
            files = await storage.list_keys(s3_prefix)
            result["files"] = files
            # Find any trajectory files
            result["trajectory_files"] = [f for f in files if "trajectory.json" in f]
        except Exception as e:
            result["error"] = f"Failed to list files: {str(e)}"

        return result


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

    async with get_session() as session:
        trial = await get_trial_for_org_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )
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

    async with get_session() as session:
        return await get_trial_trajectory_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )


@router.get("/trials/{trial_id}/result")
async def get_trial_result(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get result.json for a trial."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_trial_result_core(
            session, trial_id=trial_id, org_id=auth.org_id
        )
