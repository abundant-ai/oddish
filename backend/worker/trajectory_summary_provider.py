"""Hosted provider for durable and best-effort trajectory-summary workers.

Building a trajectory summary needs ``TrajectoryBlock`` (backend-only), so the
implementation is injected into core's post-trial classifier via
``register_trajectory_summary_provider`` at worker container load -- the same
seam pattern ``pre_trial_synth`` uses, since oddish/ can't import backend/.

The dedicated first-view job calls it strictly so failures enter normal queue
retries. ``classify_trial_and_store`` calls the same provider best-effort for
historical or re-analyzed trials; its ``components`` become the classifier's
trajectory component map.
"""

from __future__ import annotations

from oddish.db import get_session
from oddish.db.models import TrialModel
from oddish.workers.queue.trajectory_summary_handler import (
    register_trajectory_summary_provider,
)

from api.services.summarize_trajectory import get_or_generate_summary


async def provide_trajectory_summary(
    trial_id: str, triggered_by_user_id: str | None
) -> dict | None:
    """TrajectorySummaryProviderFn impl backed by get_or_generate_summary.

    Returns the summary dict stored in ``trials.trajectory_summary`` or
    ``None`` when the trial is missing or has no trajectory. Raises
    ``SummaryGenerationError`` on generation failure -- the core caller treats
    that as best-effort and classifies without a component map.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return None
        return await get_or_generate_summary(
            session,
            trial,
            triggered_by_user_id=triggered_by_user_id,
        )


# Importing this module (from backend.worker.functions) installs the hook.
register_trajectory_summary_provider(provide_trajectory_summary)
