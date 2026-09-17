"""Policy for moving a Thunder trial to ``settings.thunder_fallback_provider``.

Two handoffs exist. Both are expressed as ``JobOutcome.reroute_to`` by the
trial job handler and persisted by one atomic transaction in
``worker_job_single_job._record_reroute_outcome``; they differ only in when
they fire and in the trial state that transaction expects to find:

* **Capacity handoff** (``THUNDER_CAPACITY_UNAVAILABLE_CODE``): Harbor reported
  an exact ``sandbox_capacity_unavailable`` result. ``run_trial_job`` skips
  ordinary settlement, so the trial is still RUNNING and worker-owned when the
  handoff is recorded. Opt-in via ``ODDISH_THUNDER_CAPACITY_FALLBACK``.
* **Attempt-budget handoff** (``THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON``):
  an ordinary retryable failure settled the trial as RETRYING, and the trial
  has now failed ``settings.thunder_max_failed_attempts`` times on Thunder.
  The retry is scheduled exactly as usual, only on the fallback provider.
  Every attempt of a Thunder trial ran on Thunder (``trials.environment`` only
  ever leaves Thunder, never returns), so ``trials.attempts`` is that count.

The trial handler and the outcome recorder both consult this module so the
"which handoff, and is it enabled" decision has exactly one home. Nothing here
imports the queue runner or the trial handler; both import this module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harbor.models.environment_type import EnvironmentType

from oddish.config import settings
from oddish.core.harbor_artifacts import THUNDER_CAPACITY_UNAVAILABLE_CODE
from oddish.db import TrialStatus
from oddish.observability import ThunderHandoffOutcome, record_thunder_handoff
from oddish.workers.queue.shared import console

if TYPE_CHECKING:
    from oddish.workers.harbor.outcome import HarborOutcome

logger = logging.getLogger(__name__)

THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON = "thunder_attempt_budget_exhausted"


@dataclass(frozen=True)
class ThunderHandoff:
    """One reroute reason and the trial state its atomic transition expects."""

    reason: str
    # True when ordinary settlement already ran for the attempt: the trial is
    # RETRYING with worker ownership released, and a declined handoff must
    # degrade to the ordinary retry recording instead of failing the trial.
    settled: bool
    _enabled: Callable[[], bool]

    def enabled(self) -> bool:
        return bool(self._enabled())


THUNDER_HANDOFFS: dict[str, ThunderHandoff] = {
    handoff.reason: handoff
    for handoff in (
        ThunderHandoff(
            reason=THUNDER_CAPACITY_UNAVAILABLE_CODE,
            settled=False,
            _enabled=lambda: settings.thunder_capacity_fallback,
        ),
        ThunderHandoff(
            reason=THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
            settled=True,
            _enabled=lambda: settings.thunder_max_failed_attempts > 0,
        ),
    )
}


def thunder_handoff_for_reason(reason: str | None) -> ThunderHandoff | None:
    return THUNDER_HANDOFFS.get(reason or "")


def is_thunder_environment(environment: str | None) -> bool:
    return (environment or "").strip().lower() == EnvironmentType.THUNDER.value


def thunder_capacity_fallback_provider(
    environment: str | None,
    outcome: HarborOutcome | None,
) -> str | None:
    """Return the configured destination for an exact Thunder capacity miss."""
    if not settings.thunder_capacity_fallback:
        return None
    if not is_thunder_environment(environment):
        return None
    if (
        outcome is None
        or outcome.provider_error_code != THUNDER_CAPACITY_UNAVAILABLE_CODE
    ):
        return None
    return settings.thunder_fallback_provider


def thunder_attempt_budget_fallback_provider(
    environment: str | None,
    *,
    status: TrialStatus | str | None,
    attempts: int | None,
) -> str | None:
    """Return the destination for a Thunder trial that has failed too often.

    Applies only to an attempt that ordinary settlement left RETRYING: a FAILED
    trial has no retry to move, and a SUCCESS needs none. ``attempts`` counts
    the attempt that just failed, so the default limit of 2 hands the third
    attempt to the fallback provider.
    """
    limit = settings.thunder_max_failed_attempts
    if limit <= 0:
        return None
    if not is_thunder_environment(environment):
        return None
    # ``trials.status`` reads back as the enum in ORM code and as the upper-case
    # database label in raw rows; both mean the same settled retry.
    status_value = (
        status.value if isinstance(status, TrialStatus) else (status or "").lower()
    )
    if status_value != TrialStatus.RETRYING.value:
        return None
    if attempts is None or attempts < limit:
        return None
    return settings.thunder_fallback_provider


def emit_thunder_handoff_event(
    outcome: ThunderHandoffOutcome,
    *,
    job_id: str | None,
    trial_id: str | None,
    target: str,
    handoff: str,
    reason: str,
) -> None:
    """Log one structured handoff lifecycle line and bump the bounded counter.

    ``handoff`` is the reroute reason code (which policy fired); ``reason`` is
    the free-form detail for this event (why it was rejected, what it waits
    for). Record IDs go to the log line only, never to metric attributes.
    """
    message = (
        f"metric=thunder_handoff outcome={outcome} "
        f"job_id={job_id or 'unknown'} trial_id={trial_id or 'unknown'} "
        f"target={target} handoff={handoff} reason={reason}"
    )
    console.print(message)
    logger.info(message)
    record_thunder_handoff(outcome=outcome, target_environment=target, handoff=handoff)


__all__ = [
    "THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON",
    "THUNDER_HANDOFFS",
    "ThunderHandoff",
    "emit_thunder_handoff_event",
    "is_thunder_environment",
    "thunder_attempt_budget_fallback_provider",
    "thunder_capacity_fallback_provider",
    "thunder_handoff_for_reason",
]
