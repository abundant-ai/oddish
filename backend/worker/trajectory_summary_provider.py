"""Hosted trajectory-summary provider for post-trial QA.

Building a trajectory summary needs ``TrajectoryBlock`` (backend-only), so the
implementation is injected into core's post-trial classifier via
``register_trajectory_summary_provider`` at worker container load -- the same
seam pattern ``pre_trial_synth`` uses, since oddish/ can't import backend/.

``classify_trial_and_store`` calls the hook (best-effort) to guarantee a summary
exists before classifying; its ``components`` become the classifier's trajectory
component map. Generation is read-through cached (``get_or_generate_summary``),
so a trial already summarized by the frontend costs nothing here.
"""

from __future__ import annotations

from oddish.db import get_session
from oddish.db.models import TrialModel
from oddish.workers.jobs.handlers import (
    register_trajectory_summary_provider as register_job_provider,
)
from oddish.workers.queue.analysis_handler import (
    register_trajectory_summary_provider as register_analysis_provider,
)
from oddish.workers.queue.trajectory_summary_job import (
    register_trajectory_summary_enqueuer,
)

from api.services import summarize_trajectory
from api.services.summarize_trajectory import get_or_generate_summary


async def provide_trajectory_summary(
    trial_id: str, triggered_by_user_id: str | None = None
) -> dict | None:
    """TrajectorySummaryProviderFn impl backed by get_or_generate_summary.

    Returns the summary dict (mirrored into ``trials.trajectory_summary``) or
    ``None`` when the trial is missing or has no trajectory. Raises
    ``SummaryGenerationError`` on generation failure -- the core caller treats
    that as best-effort and classifies without a component map.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return None
        return await get_or_generate_summary(
            session, trial, triggered_by_user_id=triggered_by_user_id
        )


async def enqueue_trajectory_summary(session, trial):
    """TrajectorySummaryEnqueuer impl for the post-trial auto-enqueue.

    A pass-through on purpose: ``get_or_enqueue_summary_job`` stays the only
    writer of the job's payload and its ``schema_version`` idempotency key, so a
    later page view finds this job instead of paying for the summary again.
    """
    return await summarize_trajectory.get_or_enqueue_summary_job(session, trial)


# Importing this module (from backend.worker.functions) installs the hooks.
register_analysis_provider(provide_trajectory_summary)
register_job_provider(provide_trajectory_summary)
register_trajectory_summary_enqueuer(enqueue_trajectory_summary)
