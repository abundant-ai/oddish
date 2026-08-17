"""Auto-enqueue a trajectory-summary job for every finished agent trial.

The durable job already exists (``ANALYZER`` + ``payload.mode ==
"trajectory_summary"``, handled in ``workers.jobs.handlers``). What was missing
is anything that creates one on its own: the only enqueue site is a public page
read, so a trial on a ``run_analysis=False`` task carried no summary until a
human opened its trajectory.

Only the *policy* lives here -- the flag and which trials are worth summarizing.
The enqueue itself is delegated to the seam below, which the hosted backend fills
with ``get_or_enqueue_summary_job``. That function owns the payload shape and the
``schema_version`` idempotency key, and it must stay the single writer of them: a
second enqueue site with its own key would not find the first one's job, so a
page view after a trial finished would pay for the same summary twice. Standalone
oddish leaves the seam empty and this whole module is a no-op, which is correct --
there is no summary generator to reach.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from oddish.config import is_nop_oracle_agent, settings
from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE
from oddish.core.helpers import _has_fetchable_trajectory
from oddish.db import TrialStatus

# Takes (session, trial) and returns the one durable job for that trial and
# schema, enqueueing it if absent. Runs in the caller's transaction.
TrajectorySummaryEnqueuer = Callable[[Any, Any], Awaitable[Any]]

_enqueuer: TrajectorySummaryEnqueuer | None = None


def register_trajectory_summary_enqueuer(fn: TrajectorySummaryEnqueuer) -> None:
    """Install the hosted enqueue implementation."""
    global _enqueuer
    _enqueuer = fn


def trial_wants_trajectory_summary(trial) -> bool:
    """Whether *trial* is one a human would ever read a summary of.

    Baselines and probes are excluded because their trajectories are near-empty
    or sanctioned-harness noise, and each would still pay for a full LLM call.

    Deliberately does *not* skip a trial that already has a
    ``trajectory_summary``: a mirrored summary can be at an older
    ``schema_version``, and only the enqueuer knows the current one. Short-
    circuiting here would silently skip exactly the trials a schema bump means
    to re-summarize.
    """
    if trial.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
        return False
    if (trial.harbor_stage or "") == CANCELLED_HARBOR_STAGE:
        return False
    if trial.superseded_by_trial_id is not None:
        return False
    if trial.is_probe:
        return False
    if is_nop_oracle_agent(trial.agent):
        return False
    return _has_fetchable_trajectory(trial)


async def enqueue_trajectory_summary_job(session, trial):
    """Ensure *trial* has a summary job, or return ``None`` if it wants none.

    ``None`` when the flag is off, no enqueuer is registered, or the trial is
    ineligible. Runs in the caller's transaction so the job commits with the
    rest of the post-trial hook rather than outliving a rollback.
    """
    if not settings.auto_trajectory_summary:
        return None
    enqueuer = _enqueuer
    if enqueuer is None:
        return None
    if not trial_wants_trajectory_summary(trial):
        return None
    return await enqueuer(session, trial)


__all__ = [
    "TrajectorySummaryEnqueuer",
    "enqueue_trajectory_summary_job",
    "register_trajectory_summary_enqueuer",
    "trial_wants_trajectory_summary",
]
