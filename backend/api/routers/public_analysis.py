"""Public (unauthenticated) analysis reads for shared experiments.

These live here rather than beside their siblings in
``oddish/core/sharing/public.py`` because they are backend routes; the
share-token resolvers they use come from the ``oddish`` package, which is
the allowed import direction.

Summaries are written onto ``trials.trajectory_summary`` by the task's QA
trial import; there is no on-demand generation, so this is a plain column
read -- the same contract as the authenticated
``/trials/{trial_id}/trajectory/summary`` route.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from oddish.core.sharing.helpers import get_public_trial_for_experiment
from oddish.db import get_session

router = APIRouter(tags=["Public"])


@router.get("/public/experiments/{public_token}/trials/{trial_id}/trajectory/summary")
async def get_public_trial_trajectory_summary(public_token: str, trial_id: str) -> dict:
    """Return the trial's stored trajectory summary."""
    async with get_session() as session:
        trial = await get_public_trial_for_experiment(session, public_token, trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail="Trial not found")
        summary = trial.trajectory_summary
    if not summary:
        raise HTTPException(
            status_code=404, detail="No trajectory summary for this trial"
        )
    return summary
