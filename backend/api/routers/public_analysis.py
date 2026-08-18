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

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from api.services.blocks.analyzer.cohort.cohort_prompts import short_model_name
from oddish.core.model_display_names import (
    display_model_name,
    load_model_display_names,
)
from oddish.core.sharing.helpers import (
    get_public_task_for_experiment,
    get_public_trial_for_experiment,
)
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


def _short_name_aliases(names: dict[str, str]) -> dict[str, str]:
    """Index the alias table by the spelling the comparison actually stores.

    ``load_model_display_names`` keys on the full id (``anthropic/claude-opus-4-8``)
    because that is what ``trials.model`` holds. The comparison does not: both
    ``models[].model`` and ``trial_models`` are written through
    ``short_model_name``, which strips the provider and region
    (``global.anthropic.claude-opus-4-8`` -> ``claude-opus-4-8``). Masking on the
    full id alone therefore matches nothing and publishes the real names.

    A short name can collide -- ``global.anthropic.…`` and ``us.anthropic.…``
    reduce to one string. When two aliases disagree on a collision the key is
    dropped rather than guessed: naming the wrong model misattributes the
    behaviour the analysis describes, which is worse than leaving the short
    name showing.
    """
    short: dict[str, str] = {}
    dropped: set[str] = set()
    for key in sorted(names):
        alias = names[key]
        name = short_model_name(key)
        if not name or name == key:
            continue
        if short.setdefault(name, alias) != alias:
            dropped.add(name)
    for name in dropped:
        short.pop(name, None)
        logger.warning(
            "model display names disagree for short name %r; leaving it unmasked",
            name,
        )
    return {**short, **names}


def _mask_models(comparison: dict, names: dict[str, str]) -> dict:
    """Rewrite the comparison's model ids through the operator alias table.

    Mutating a copy, not the argument: the dict is an ``AnalyzerBlock`` row's
    ``output``, and the session it came from is still open.
    """
    if not names:
        return comparison
    names = _short_name_aliases(names)
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


async def _version_is_in_experiment(
    session, experiment_id: str, task_version_id: str
) -> bool:
    """Whether the shared experiment has a trial on this task version.

    Membership goes through ``trial_in_experiment`` -- a collection gathers
    trials that keep the ``experiment_id`` of wherever they ran, so an FK-only
    filter misses them.
    """
    from oddish.core.experiment_membership import trial_in_experiment
    from oddish.db.models import TrialModel

    return (
        await session.execute(
            select(TrialModel.id)
            .where(
                TrialModel.task_version_id == task_version_id,
                TrialModel.is_probe.is_(False),
                trial_in_experiment(experiment_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


@router.get("/public/experiments/{public_token}/tasks/{task_id}/agent-capabilities")
async def get_public_task_agent_capabilities(
    public_token: str,
    task_id: str,
    response: Response,
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
    from api.services.agent_capabilities import (
        analysis_is_eligible,
        enqueue_analysis,
        load_stored_analysis,
    )

    async with get_session() as session:
        resolved = await get_public_task_for_experiment(session, public_token, task_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Task not found")
        experiment, task, _ = resolved
        if not task.current_version_id:
            raise HTTPException(status_code=404, detail="Task not found")
        version_id = task.current_version_id
        if version is not None:
            from oddish.db.models import TaskVersionModel

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
        # The token publishes an experiment, not a task's whole history. Without
        # this, `?version=` walks every version the task ever had -- including
        # ones this experiment never ran, whose trial ids, models and trajectory
        # quotes the share was never meant to carry. Bound it to the versions
        # the share actually displays.
        if not await _version_is_in_experiment(session, experiment.id, version_id):
            raise HTTPException(status_code=404, detail="Task version not found")
        # A share has nothing to fall back on, so a comparison whose trial set
        # has since moved is served rather than withheld -- flagged, and with a
        # durable rebuild asked for below. A cold miss uses the same queue and
        # returns 202; neither path runs Claude Code in the API container.
        stored = await load_stored_analysis(
            session,
            version_id,
            task_id=task.id,
            allow_cohort_drift=True,
            experiment_id=experiment.id,
        )
        if stored is None:
            if not await analysis_is_eligible(session, version_id, experiment.id):
                raise HTTPException(
                    status_code=404,
                    detail="No completed trajectories available to analyze",
                )
            from oddish.db.models import TaskVersionModel

            await session.execute(
                select(TaskVersionModel.id)
                .where(TaskVersionModel.id == version_id)
                .with_for_update()
            )
            await enqueue_analysis(
                session,
                task_id=task.id,
                task_version_id=version_id,
                task_name=task.name,
                org_id=task.org_id,
                triggered_by_user_id=None,
                experiment_id=experiment.id,
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return {"status": "queued", "task_version_id": version_id}
        regenerating = False
        if stored.stale:
            from oddish.db.models import TaskVersionModel

            await session.execute(
                select(TaskVersionModel.id)
                .where(TaskVersionModel.id == version_id)
                .with_for_update()
            )
            await enqueue_analysis(
                session,
                task_id=task.id,
                task_version_id=version_id,
                task_name=task.name,
                org_id=task.org_id,
                triggered_by_user_id=None,
                experiment_id=experiment.id,
            )
            regenerating = True
        names = await load_model_display_names(session)
    # Stamped at serve time, matching the authenticated route: the id is what
    # the UI addresses a version by, while this route takes the number.
    return {
        **_mask_models(stored.output, names),
        "task_version_id": version_id,
        "stale": stored.stale,
        "regenerating": regenerating,
    }
