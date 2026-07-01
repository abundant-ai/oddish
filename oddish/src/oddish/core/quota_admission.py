from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    inflight_count,
    start_of_today_utc,
    sum_cost_usd,
)

if TYPE_CHECKING:
    from oddish.schemas import TaskSubmission

logger = logging.getLogger(__name__)


class QuotaExceeded(HTTPException):
    def __init__(self, used_usd, limit_usd, period: str = "daily") -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"Over your daily budget: used ${float(used_usd):.2f} of "
                    f"${float(limit_usd):.2f} ({period}). Ask an org admin to "
                    "raise your quota."
                ),
                "used_usd": float(used_usd),
                "limit_usd": float(limit_usd),
                "period": period,
            },
        )


class Unattributed(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            detail={
                "message": (
                    "This run can't be attributed to a user. Link your GitHub at "
                    "oddish.app so your usage can be billed."
                )
            },
        )


def _log_would_block(org_id, billed_user_id, used, limit, *, reason: str) -> None:
    logger.warning(
        "metric=quota.would_block reason=%s org_id=%s billed_user_id=%s "
        "used=%s limit=%s",
        reason,
        org_id,
        billed_user_id,
        used,
        limit,
    )


async def admit_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    count: int,
) -> None:
    mode = settings.quota_mode
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return

    if billed_user_id is None:
        if mode == QuotaMode.ENFORCE:
            raise Unattributed()
        _log_would_block(org_id, None, None, None, reason="unattributed")
        return

    effective_limit_usd = await get_effective_limit(session, org_id, billed_user_id)
    used_usd = await sum_cost_usd(
        session, org_id, billed_user_id, start_of_today_utc()
    )
    reserved_usd = (
        await inflight_count(session, org_id, billed_user_id) + count
    ) * settings.pending_trial_reservation_usd

    if used_usd + reserved_usd >= effective_limit_usd:
        if mode == QuotaMode.ENFORCE:
            raise QuotaExceeded(used_usd, effective_limit_usd)
        _log_would_block(
            org_id, billed_user_id, used_usd, effective_limit_usd, reason="over_budget"
        )


async def admit_submission_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    submission: "TaskSubmission",
) -> None:
    await admit_trials(session, org_id, billed_user_id, count=len(submission.trials))
