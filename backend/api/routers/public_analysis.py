"""Public (unauthenticated) analysis reads for shared experiments.

These live here rather than beside their siblings in
``oddish/core/sharing/public.py`` because they read the hosted analysis
services (``api.services.*``), and the ``oddish`` package may not import the
backend. The share-token resolvers go the other way, which is allowed.

Trajectory-summary misses trigger a trial-scoped durable job. The request never
holds an API connection open for paid generation, and repeated views coalesce
by trial and summary schema.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status

from oddish.core.sharing.helpers import get_public_trial_for_experiment
from oddish.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Public"])


@router.get("/public/experiments/{public_token}/trials/{trial_id}/trajectory/summary")
async def get_public_trial_trajectory_summary(
    public_token: str, trial_id: str, response: Response
) -> dict:
    """Return a stored public summary or durably queue its generation."""
    from api.services.summarize_trajectory import (
        get_or_enqueue_summary_job,
        load_stored_summary,
    )
    from oddish.core.helpers import _has_fetchable_trajectory
    from oddish.db.models import WorkerJobStatus

    async with get_session() as session:
        trial = await get_public_trial_for_experiment(session, public_token, trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail="Trial not found")
        summary = await load_stored_summary(session, trial)
        if summary is not None:
            return summary
        if not _has_fetchable_trajectory(trial):
            raise HTTPException(
                status_code=404,
                detail="No completed trajectory available to summarize",
            )
        job = await get_or_enqueue_summary_job(session, trial)
        if job.status == WorkerJobStatus.SUCCESS:
            # The worker may have committed between the first cache read and
            # our row lock. Re-read before reporting an inconsistent success.
            summary = await load_stored_summary(session, trial)
            if summary is not None:
                return summary
            raise HTTPException(
                status_code=502,
                detail="Summary generation finished without a stored summary",
            )
        if job.status == WorkerJobStatus.FAILED:
            logger.warning(
                "public trajectory summary job %s failed for trial %s: %s",
                job.id,
                trial.id,
                job.error_message,
            )
            raise HTTPException(
                status_code=502,
                detail="Summary generation failed",
            )
        if job.status == WorkerJobStatus.CANCELLED:
            raise HTTPException(
                status_code=503,
                detail="Summary generation was cancelled",
            )
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "3"
        return {
            "status": job.status.value.lower(),
            "job_id": job.id,
            "retry_after_ms": 3000,
        }
