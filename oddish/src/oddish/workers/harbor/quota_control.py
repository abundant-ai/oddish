from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from oddish.config import settings
from oddish.core.quota_pause import quota_pause_requested
from oddish.db import TrialModel, TrialStatus, get_session

logger = logging.getLogger(__name__)


@dataclass
class QuotaPauseController:
    requested: bool = False
    paused: bool = False
    status_synced: bool = True
    resume_on_exit: bool = False


_controllers: dict[str, QuotaPauseController] = {}


class QuotaPauseControlError(RuntimeError):
    pass


def set_quota_pause_requested(trial_id: str, requested: bool) -> None:
    controller = _controllers.get(trial_id)
    if controller is not None:
        controller.requested = requested


async def _record_pause_state(trial_id: str, paused: bool) -> None:
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id, with_for_update=True)
        if trial is None:
            return
        if paused and trial.status == TrialStatus.RUNNING:
            trial.status = TrialStatus.PAUSED
        elif not paused and trial.status == TrialStatus.PAUSED:
            trial.status = TrialStatus.RUNNING


async def control_job_quota_pause(
    job: Any,
    *,
    trial_id: str,
    org_id: str | None,
    billed_user_id: str | None,
    stop: asyncio.Event,
    controller: QuotaPauseController,
) -> None:
    last_refresh = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_refresh >= settings.quota_pause_refresh_seconds:
            last_refresh = now
            try:
                async with get_session() as session:
                    controller.requested = await quota_pause_requested(
                        session,
                        org_id=org_id,
                        billed_user_id=billed_user_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to refresh quota state for trial_id=%s; "
                    "preserving requested=%s",
                    trial_id,
                    controller.requested,
                )

        if controller.requested != controller.paused:
            action = "pause Harbor job" if controller.requested else "resume Harbor job"
            try:
                if controller.requested:
                    logger.warning("metric=quota.job_pausing trial_id=%s", trial_id)
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

            controller.paused = controller.requested
            controller.status_synced = False
            if controller.paused:
                last_refresh = now
                logger.warning("metric=quota.job_paused trial_id=%s", trial_id)
            else:
                logger.info("metric=quota.job_resumed trial_id=%s", trial_id)

        if not controller.status_synced:
            try:
                await _record_pause_state(trial_id, controller.paused)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to record quota pause state for trial_id=%s paused=%s",
                    trial_id,
                    controller.paused,
                )
            else:
                controller.status_synced = True

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.quota_pause_poll_seconds
            )
        except TimeoutError:
            pass


async def run_job_with_quota_control(
    job: Any,
    *,
    trial_id: str,
    org_id: str,
    billed_user_id: str | None,
) -> Any:
    controller = QuotaPauseController()
    _controllers[trial_id] = controller
    stop = asyncio.Event()
    run_task = asyncio.create_task(job.run())
    control_task = asyncio.create_task(
        control_job_quota_pause(
            job,
            trial_id=trial_id,
            org_id=org_id,
            billed_user_id=billed_user_id,
            stop=stop,
            controller=controller,
        )
    )
    try:
        completed, _ = await asyncio.wait(
            {run_task, control_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task in completed:
            result = await run_task
            controller.resume_on_exit = True
            stop.set()
            (control_result,) = await asyncio.gather(
                control_task, return_exceptions=True
            )
            if isinstance(control_result, BaseException):
                logger.warning(
                    "Ignoring quota pause control failure after Harbor completed "
                    "for trial_id=%s error=%s: %s",
                    trial_id,
                    type(control_result).__name__,
                    control_result,
                )
            return result

        await control_task
        result = await run_task
        controller.resume_on_exit = True
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
        try:
            if controller.resume_on_exit:
                if controller.paused:
                    try:
                        await job.resume()
                    except Exception:
                        logger.exception(
                            "Failed to resume quota-paused Harbor job after normal "
                            "completion for trial_id=%s",
                            trial_id,
                        )
                    else:
                        controller.paused = False
                        controller.status_synced = False
                        logger.info(
                            "metric=quota.job_resumed trial_id=%s reason=job_complete",
                            trial_id,
                        )

                if not controller.status_synced:
                    try:
                        await _record_pause_state(trial_id, controller.paused)
                    except Exception:
                        logger.exception(
                            "Failed to record final quota pause state for "
                            "trial_id=%s paused=%s",
                            trial_id,
                            controller.paused,
                        )
                    else:
                        controller.status_synced = True
        finally:
            _controllers.pop(trial_id, None)
