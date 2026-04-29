"""Local in-process trial runner. Used when ``ODDISH_LOCAL_MODE=1``.

Bypasses the Modal queue and runs trials directly via Harbor's Python
API, talking to a local Docker daemon for the env. State is written to
the same Postgres rows the Modal worker would update, so the rest of
the stack (FE, analysis pipeline) sees a normal trial.

Task 7 lands the status-transition skeleton with a stubbed
``_run_harbor_trial``; Task 8 fills in real Harbor execution and the
task-mutation overlay (copy task to ``/tmp/freeform-X``, prepend
``extra_instructions`` to ``instruction.md``, run ``harbor.trial.Trial``
against the temp dir).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from oddish.db import TrialModel, TrialStatus, get_session

logger = logging.getLogger(__name__)


async def run_trial_locally(trial_id: str, *, dry_run: bool = False) -> None:
    """Execute a freeform trial in-process and mirror status to the DB.

    Status transitions: ``QUEUED`` -> ``RUNNING`` -> ``SUCCESS``
    (or ``FAILED`` on exception, with ``error_message`` populated).

    When ``dry_run`` is True, skips the actual Harbor call. Used in
    tests to exercise the status-transition path without spinning up
    Docker.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(f"Trial {trial_id} not found")
        trial.status = TrialStatus.RUNNING
        trial.started_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> RUNNING", trial_id)

    try:
        if not dry_run:
            await _run_harbor_trial(trial_id)
    except Exception as exc:
        logger.exception("local_runner: trial %s failed", trial_id)
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is not None:
                trial.status = TrialStatus.FAILED
                trial.error_message = str(exc)
                trial.finished_at = datetime.now(timezone.utc)
        raise

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(
                f"Trial {trial_id} disappeared mid-run; cannot mark SUCCESS"
            )
        trial.status = TrialStatus.SUCCESS
        trial.finished_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> SUCCESS", trial_id)


async def _run_harbor_trial(trial_id: str) -> None:
    """Execute the trial against a local Harbor instance.

    Stub for Task 8 -- will instantiate ``harbor.trial.Trial``, apply
    the task-mutation overlay (extra_instructions prepended to
    ``instruction.md``), run it against a local Docker daemon, and
    persist artifacts to the configured S3/MinIO bucket.
    """
    raise NotImplementedError(
        "Real Harbor execution lands in Task 8 of the freeform-agent rollout"
    )
