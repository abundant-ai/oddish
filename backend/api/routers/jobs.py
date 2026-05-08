from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from oddish.core.jobs import (
    get_job_core,
    list_job_trials_core,
    list_jobs_core,
)
from oddish.db import get_session
from oddish.schemas import JobListResponse, JobResponse

from auth import APIKeyScope, AuthContext, require_auth


router = APIRouter(tags=["Jobs"])


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    auth: Annotated[AuthContext, Depends(require_auth)],
    kind: str | None = Query(None, description="Filter by JobKind value"),
    triggered_by_experiment_id: str | None = Query(
        None,
        description="Filter to jobs that were triggered by a specific experiment",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JobListResponse:
    """List Jobs (org-scoped) with aggregate trial counts."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await list_jobs_core(
            session,
            org_id=auth.org_id,
            kind=kind,
            triggered_by_experiment_id=triggered_by_experiment_id,
            limit=limit,
            offset=offset,
        )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> JobResponse:
    """Get a single Job by id (org-scoped) with aggregate trial counts."""
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        return await get_job_core(session, job_id=job_id, org_id=auth.org_id)


@router.get("/jobs/{job_id}/trials")
async def list_job_trials(
    job_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 500,
) -> list[dict]:
    """List trials produced by a Job (compact representation)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_job_trials_core(
            session, job_id=job_id, org_id=auth.org_id, limit=limit
        )
