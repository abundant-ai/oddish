from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_org_cost_usd,
)

logger = logging.getLogger(__name__)


class QuotaExceeded(HTTPException):
    def __init__(self, used_usd, reserved_usd, limit_usd) -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"Over your 24h budget: used ${float(used_usd):.2f} + "
                    f"${float(reserved_usd):.2f} reserved of "
                    f"${float(limit_usd):.2f}. Spend frees 24h after each "
                    "run finishes. Ask an org admin to raise your quota."
                ),
                "used_usd": float(used_usd),
                "reserved_usd": float(reserved_usd),
                "limit_usd": float(limit_usd),
            },
        )


class OrgQuotaExceeded(HTTPException):
    def __init__(self, used_usd, reserved_usd, limit_usd) -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"Your organization is over its monthly budget: used "
                    f"${float(used_usd):.2f} + ${float(reserved_usd):.2f} "
                    f"reserved of ${float(limit_usd):.2f} (monthly). The budget "
                    "resets on the 1st (UTC). Ask an org admin to raise the "
                    "org quota."
                ),
                "used_usd": float(used_usd),
                "reserved_usd": float(reserved_usd),
                "limit_usd": float(limit_usd),
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


def _log_would_block(
    org_id, billed_user_id, used, reserved, limit, *, reason: str
) -> None:
    logger.warning(
        "metric=quota.would_block reason=%s org_id=%s billed_user_id=%s "
        "used=%s reserved=%s limit=%s",
        reason,
        org_id,
        billed_user_id,
        used,
        reserved,
        limit,
    )


def _raise_or_log_over_budget(
    org_id,
    billed_user_id,
    used,
    reserved,
    limit,
    *,
    enforce: bool,
    exc_type,
    reason: str,
) -> None:
    if used + reserved < limit:
        return
    if enforce:
        raise exc_type(used, reserved, limit)
    _log_would_block(org_id, billed_user_id, used, reserved, limit, reason=reason)


async def _check_user_quota(
    session: AsyncSession,
    org_id: str,
    billed_user_id: str,
    count: int,
    *,
    enforce: bool,
) -> None:
    limit = await get_effective_limit(session, org_id, billed_user_id)
    used = await sum_cost_usd(session, org_id, billed_user_id, quota_window_start())
    reserved = (
        await inflight_reserved_usd(session, org_id, billed_user_id)
        + count * settings.pending_trial_reservation_usd
    )
    _raise_or_log_over_budget(
        org_id,
        billed_user_id,
        used,
        reserved,
        limit,
        enforce=enforce,
        exc_type=QuotaExceeded,
        reason="over_budget",
    )


async def _check_org_quota(
    session: AsyncSession,
    org_id: str,
    billed_user_id: str | None,
    count: int,
    *,
    enforce: bool,
) -> None:
    limit = await get_effective_org_limit(session, org_id)
    if limit is None:
        return
    used = await sum_org_cost_usd(session, org_id, start_of_month_utc())
    reserved = (
        await org_inflight_reserved_usd(session, org_id)
        + count * settings.pending_trial_reservation_usd
    )
    _raise_or_log_over_budget(
        org_id,
        billed_user_id,
        used,
        reserved,
        limit,
        enforce=enforce,
        exc_type=OrgQuotaExceeded,
        reason="org_over_budget",
    )


async def admit_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    count: int,
    *,
    allow_unattributed: bool = False,
) -> None:
    mode = settings.quota_mode
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return
    enforce = mode == QuotaMode.ENFORCE

    if billed_user_id is None:
        if enforce and not allow_unattributed:
            raise Unattributed()
        if mode == QuotaMode.SHADOW:
            _log_would_block(org_id, None, None, None, None, reason="unattributed")
    else:
        await _check_user_quota(session, org_id, billed_user_id, count, enforce=enforce)

    await _check_org_quota(session, org_id, billed_user_id, count, enforce=enforce)
