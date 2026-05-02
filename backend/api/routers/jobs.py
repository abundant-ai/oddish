from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.jobs import get_job_core, list_job_trials_core, list_jobs_core
from oddish.db import BatchJobStatus, get_session
from oddish.schemas import JobResponse, TrialResponse


router = APIRouter(tags=["Jobs"])


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    auth: Annotated[AuthContext, Depends(require_auth)],
    status: BatchJobStatus | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[JobResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_jobs_core(
            session,
            org_id=auth.org_id,
            status=status,
            limit=limit,
        )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> JobResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await get_job_core(session, job_id=job_id, org_id=auth.org_id)


@router.get("/jobs/{job_id}/trials", response_model=list[TrialResponse])
async def list_job_trials(
    job_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = Query(500, ge=1, le=2000),
) -> list[TrialResponse]:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_job_trials_core(
            session,
            job_id=job_id,
            org_id=auth.org_id,
            limit=limit,
        )
