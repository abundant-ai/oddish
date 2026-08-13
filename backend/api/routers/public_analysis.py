"""Public (unauthenticated) analysis reads for shared experiments.

These live here rather than beside their siblings in
``oddish/core/sharing/public.py`` because they read the hosted analysis
services (``api.services.*``), and the ``oddish`` package may not import the
backend. The share-token resolvers go the other way, which is allowed.

Both routes are cache reads. Their authenticated counterparts generate on a
miss -- a Claude call per trajectory summary, a claude-code run per comparison
-- and neither may be reachable without a login: the spend is unbounded by
anything the caller has to hold, and the comparison additionally parks one of
an API container's three connections for minutes. A miss here is a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from oddish.core.model_display_names import (
    display_model_name,
    load_model_display_names,
)
from oddish.core.sharing.helpers import (
    get_public_task_for_experiment,
    get_public_trial_for_experiment,
)
from oddish.db import get_session

router = APIRouter(tags=["Public"])


@router.get("/public/experiments/{public_token}/trials/{trial_id}/trajectory/summary")
async def get_public_trial_trajectory_summary(
    public_token: str, trial_id: str
) -> dict:
    """The stored trajectory summary for a public trial."""
    from api.services.summarize_trajectory import load_stored_summary

    async with get_session() as session:
        trial = await get_public_trial_for_experiment(session, public_token, trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail="Trial not found")
        summary = await load_stored_summary(session, trial.id)
    if summary is None:
        raise HTTPException(
            status_code=404, detail="No trajectory summary for this trial"
        )
    return summary


def _mask_models(comparison: dict, names: dict[str, str]) -> dict:
    """Rewrite the comparison's model ids through the operator alias table.

    Mutating a copy, not the argument: the dict is an ``AnalyzerBlock`` row's
    ``output``, and the session it came from is still open.
    """
    if not names:
        return comparison
    masked = dict(comparison)
    models = masked.get("models")
    if isinstance(models, dict):
        masked["models"] = {
            side: [
                {**entry, "model": display_model_name(entry.get("model"), names)}
                if isinstance(entry, dict)
                else entry
                for entry in entries
            ]
            if isinstance(entries, list)
            else entries
            for side, entries in models.items()
        }
    trial_models = masked.get("trial_models")
    if isinstance(trial_models, dict):
        masked["trial_models"] = {
            trial_id: display_model_name(model, names)
            for trial_id, model in trial_models.items()
        }
    return masked


@router.get("/public/experiments/{public_token}/tasks/{task_id}/cohort-comparison")
async def get_public_task_cohort_comparison(
    public_token: str,
    task_id: str,
    version: int | None = Query(
        None,
        description=(
            "Compare this task version instead of the current one. A share "
            "page pins the version its trials ran on, so without this an "
            "older version would show the current version's comparison."
        ),
    ),
) -> dict:
    """The stored successful-vs-failing comparison for a public task version."""
    from api.services.cohort_comparison import load_stored_comparison

    async with get_session() as session:
        resolved = await get_public_task_for_experiment(session, public_token, task_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Task not found")
        _, task, _ = resolved
        if not task.current_version_id:
            raise HTTPException(status_code=404, detail="Task not found")
        version_id = task.current_version_id
        if version is not None:
            from oddish.db.models import TaskVersionModel
            from sqlalchemy import select

            version_id = (
                await session.execute(
                    select(TaskVersionModel.id).where(
                        TaskVersionModel.task_id == task.id,
                        TaskVersionModel.version == version,
                    )
                )
            ).scalar_one_or_none()
            if version_id is None:
                raise HTTPException(status_code=404, detail="Task version not found")
        comparison = await load_stored_comparison(
            session, version_id, task_id=task.id
        )
        if comparison is None:
            raise HTTPException(
                status_code=404, detail="No comparison stored for this version"
            )
        names = await load_model_display_names(session)
    # Stamped at serve time, matching the authenticated route: the id is what
    # the UI addresses a version by, while this route takes the number.
    return {**_mask_models(comparison, names), "task_version_id": version_id}
