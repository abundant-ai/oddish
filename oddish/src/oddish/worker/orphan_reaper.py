"""Orphan-trial reaper.

When the backend restarts mid-trial OR a Harbor setup phase hangs and the
runner asyncio task is silently lost, trials sit in `status=RUNNING` forever
because nothing ever flips them to SUCCESS/FAILED. This module reaps those
trials at startup time.

A trial is considered orphaned when:
  - status == RUNNING
  - AND no docker container exists whose name ends with
    `<trial_name_lowercased>-main-1`
    (the convention Harbor uses for its docker-compose project).

Reaped trials are marked FAILED with an explanatory error_message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from oddish.db import TrialModel, TrialStatus, get_session

logger = logging.getLogger(__name__)


REAP_REASON = (
    "Trial was orphaned: backend restarted or Harbor setup hung mid-trial, "
    "and the docker container is no longer present. The status was stuck on "
    "RUNNING because no code path ever flipped it. Submit a fresh probe to retry."
)


async def _running_container_names() -> set[str]:
    """Return the set of currently running docker container names."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except Exception as exc:
        logger.warning("orphan reaper: docker ps failed: %s", exc)
        return set()
    return {line.strip() for line in stdout.decode().splitlines() if line.strip()}


def _has_matching_container(trial_name: str, container_names: set[str]) -> bool:
    """Match Harbor's docker-compose convention: <trial_name_lowercased>-main-1."""
    expected_suffix = f"{trial_name.lower()}-main-1"
    return any(name.lower().endswith(expected_suffix) for name in container_names)


async def reap_orphan_trials() -> int:
    """Find RUNNING trials whose container has gone, mark them FAILED.

    Returns the number of trials reaped. Safe to call repeatedly — only
    flips trials whose status is currently RUNNING and whose container is
    actually missing.
    """
    container_names = await _running_container_names()

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(TrialModel).where(TrialModel.status == TrialStatus.RUNNING)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return 0

    candidates = [
        trial
        for trial in rows
        if not _has_matching_container(trial.name, container_names)
    ]
    if not candidates:
        return 0

    reaped = 0
    now = datetime.now(timezone.utc)
    for orphan in candidates:
        # Re-check inside the session to avoid racing with a runner that
        # finished between our list query and now.
        async with get_session() as session:
            row = await session.get(TrialModel, orphan.id)
            if row is None or row.status != TrialStatus.RUNNING:
                continue
            row.status = TrialStatus.FAILED
            row.finished_at = now
            row.error_message = REAP_REASON
            await session.commit()
        reaped += 1
        logger.warning(
            "orphan reaper: reaped trial id=%s name=%s (no matching container)",
            orphan.id,
            orphan.name,
        )

    return reaped
