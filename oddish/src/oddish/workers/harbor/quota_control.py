from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from oddish.config import settings
from oddish.core.quota_pause import quota_pause_requested
from oddish.db import get_session

logger = logging.getLogger(__name__)
_requests: dict[str, bool] = {}


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
            try:
                now = time.monotonic()
                if (
                    paused
                    and now - last_refresh >= settings.quota_pause_refresh_seconds
                ):
                    await _refresh_request(trial_id, org_id, billed_user_id)
                    last_refresh = now

                requested = _requests[trial_id]
                if requested and not paused:
                    await job.pause()
                    paused = True
                    last_refresh = now
                    logger.warning("metric=quota.job_paused trial_id=%s", trial_id)
                elif paused and not requested:
                    await job.resume()
                    paused = False
                    logger.info("metric=quota.job_resumed trial_id=%s", trial_id)
            except asyncio.CancelledError:
                raise
            except NotImplementedError:
                logger.warning(
                    "Quota pause requested for an environment that does not support it"
                )
                return
            except Exception:
                logger.exception("Quota pause control failed for trial_id=%s", trial_id)

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.quota_pause_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        _requests.pop(trial_id, None)
