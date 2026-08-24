from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from oddish.config import settings
from oddish.core.quota_pause import quota_pause_requested
from oddish.db import TrialModel, TrialStatus, get_session

logger = logging.getLogger(__name__)
_requests: dict[str, bool] = {}


class QuotaPauseControlError(RuntimeError):
    pass


def set_quota_pause_requested(trial_id: str, requested: bool) -> None:
    _requests[trial_id] = requested


async def _refresh_request(
    trial_id: str, org_id: str | None, billed_user_id: str | None
) -> None:
    async with get_session() as session:
        _requests[trial_id] = await quota_pause_requested(
            session,
            org_id=org_id,
            billed_user_id=billed_user_id,
        )


async def _record_pause_state(trial_id: str, paused: bool) -> None:
    try:
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id, with_for_update=True)
            if trial is None:
                return
            if paused and trial.status == TrialStatus.RUNNING:
                trial.status = TrialStatus.PAUSED
            elif not paused and trial.status == TrialStatus.PAUSED:
                trial.status = TrialStatus.RUNNING
    except Exception:
        logger.exception(
            "Failed to record quota pause state for trial_id=%s paused=%s",
            trial_id,
            paused,
        )


async def control_job_quota_pause(
    job: Any,
    *,
    trial_id: str,
    org_id: str | None,
    billed_user_id: str | None,
    stop: asyncio.Event,
) -> None:
    paused = False
    last_refresh = 0.0
    _requests.setdefault(trial_id, False)
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now - last_refresh >= settings.quota_pause_refresh_seconds:
                last_refresh = now
                try:
                    await _refresh_request(trial_id, org_id, billed_user_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to refresh quota state for trial_id=%s; "
                        "preserving requested=%s",
                        trial_id,
                        _requests[trial_id],
                    )

            requested = _requests[trial_id]
            if requested != paused:
                action = "pause Harbor job" if requested else "resume Harbor job"
                try:
                    if requested:
                        logger.warning(
                            "metric=quota.job_pausing trial_id=%s", trial_id
                        )
                        await job.pause()
                    else:
                        await job.resume()
                except asyncio.CancelledError:
                    raise
                except NotImplementedError:
                    logger.warning(
                        "Harbor environment does not support quota pause/resume "
                        "for trial_id=%s",
                        trial_id,
                    )
                    return
                except Exception as exc:
                    logger.exception(
                        "Quota pause control failed for trial_id=%s action=%s",
                        trial_id,
                        action,
                    )
                    raise QuotaPauseControlError(
                        f"Failed to {action} for trial {trial_id}: {exc}"
                    ) from exc

                paused = requested
                await _record_pause_state(trial_id, paused)
                if paused:
                    last_refresh = now
                    logger.warning("metric=quota.job_paused trial_id=%s", trial_id)
                else:
                    logger.info("metric=quota.job_resumed trial_id=%s", trial_id)

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.quota_pause_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        _requests.pop(trial_id, None)


async def run_job_with_quota_control(
    job: Any,
    *,
    trial_id: str,
    org_id: str,
    billed_user_id: str | None,
) -> Any:
    stop = asyncio.Event()
    run_task = asyncio.create_task(job.run())
    control_task = asyncio.create_task(
        control_job_quota_pause(
            job,
            trial_id=trial_id,
            org_id=org_id,
            billed_user_id=billed_user_id,
            stop=stop,
        )
    )
    try:
        completed, _ = await asyncio.wait(
            {run_task, control_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if control_task in completed:
            await control_task
        result = await run_task
        stop.set()
        await control_task
        return result
    finally:
        stop.set()
        for task in (run_task, control_task):
            if not task.done():
                task.cancel()
        _, pending = await asyncio.wait(
            {run_task, control_task},
            timeout=settings.quota_pause_cancel_timeout_seconds,
        )
        if pending:
            logger.error(
                "metric=quota.job_cancel_timeout trial_id=%s pending_tasks=%d",
                trial_id,
                len(pending),
            )
