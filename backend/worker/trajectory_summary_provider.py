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
from oddish.workers.queue.analysis_handler import register_trajectory_summary_provider

from api.services.summarize_trajectory import get_or_generate_summary


async def provide_trajectory_summary(trial_id: str) -> dict | None:
    """TrajectorySummaryProviderFn impl backed by get_or_generate_summary.

    Returns the summary dict (mirrored into ``trials.trajectory_summary``) or
    ``None`` when the trial is missing or has no trajectory. Raises
    ``SummaryGenerationError`` on generation failure -- the core caller treats
    that as best-effort and classifies without a component map.

    The session spans generation but holds no connection across it:
    ``get_or_generate_summary`` commits before the LLM call, which is what keeps
    this NullPool-safe on workers.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return None
        return await get_or_generate_summary(session, trial)


# Importing this module (from backend.worker.functions) installs the hook.
register_trajectory_summary_provider(provide_trajectory_summary)
