from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    api_key_inflight_reserved_usd,
    get_api_key_limit,
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
    sum_api_key_cost_usd,
    sum_cost_usd,
    sum_org_cost_usd,
    unattributed_cost_usd,
    unattributed_inflight_reserved_usd,
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


class APIKeyQuotaExceeded(HTTPException):
    def __init__(self, used_usd, reserved_usd, limit_usd) -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"This API key is over its 24h budget: used "
                    f"${float(used_usd):.2f} + ${float(reserved_usd):.2f} "
                    f"reserved of ${float(limit_usd):.2f}. Edit the key's "
                    "limit in Oddish settings or wait for spend to age out."
                ),
                "used_usd": float(used_usd),
                "reserved_usd": float(reserved_usd),
                "limit_usd": float(limit_usd),
            },
        )


class UnattributedPoolExceeded(HTTPException):
    def __init__(self, used_usd, reserved_usd, limit_usd) -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"This organization is over its unattributed-spend ceiling: "
                    f"used ${float(used_usd):.2f} + ${float(reserved_usd):.2f} "
                    f"reserved of ${float(limit_usd):.2f} over the last 24h. This "
                    "pool covers runs whose payer could not be resolved, so it "
                    "clears by linking GitHub accounts at oddish.app (or by an "
                    "admin raising ODDISH_UNATTRIBUTED_POOL_LIMIT_USD), not by "
                    "raising an individual quota."
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


async def _check_api_key_quota(
    session: AsyncSession,
    org_id: str,
    api_key_id: str,
    count: int,
    *,
    enforce: bool,
) -> None:
    limit = await get_api_key_limit(session, org_id, api_key_id)
    if limit is None:
        return
    used = await sum_api_key_cost_usd(session, org_id, api_key_id, quota_window_start())
    reserved = (
        await api_key_inflight_reserved_usd(session, org_id, api_key_id)
        + count * settings.pending_trial_reservation_usd
    )
    _raise_or_log_over_budget(
        org_id,
        None,
        used,
        reserved,
        limit,
        enforce=enforce,
        exc_type=APIKeyQuotaExceeded,
        reason="api_key_over_budget",
    )


async def _check_unattributed_quota(
    session: AsyncSession,
    org_id: str,
    count: int,
    *,
    enforce: bool,
) -> None:
    # A trial with no resolvable payer has no per-user cap to charge, and the org
    # monthly cap ships unset, so the retry path would otherwise spend freely.
    # The pool drains only by 24h aging (a retry copies billed_user_id=None, so
    # it re-enters the pool), and no per-user override can raise it -- so it
    # stays off until an operator sets a ceiling they actually want enforced.
    limit = settings.unattributed_pool_limit_usd
    used = await unattributed_cost_usd(session, org_id, quota_window_start())
    reserved = (
        await unattributed_inflight_reserved_usd(session, org_id)
        + count * settings.pending_trial_reservation_usd
    )
    if limit is not None:
        _raise_or_log_over_budget(
            org_id,
            None,
            used,
            reserved,
            limit,
            enforce=enforce,
            exc_type=UnattributedPoolExceeded,
            reason="unattributed_over_budget",
        )
    # Past the check, so this really was admitted -- spend nobody can be billed
    # for is worth surfacing even under a ceiling that allowed it, because the fix
    # is to repair attribution rather than raise a cap. In SHADOW an over-pool
    # admission emits would_block AND this; that pair is precisely the signal that
    # enforcement would have stopped it.
    logger.warning(
        "metric=quota.admitted reason=unattributed_admitted org_id=%s count=%s "
        "used=%s reserved=%s limit=%s",
        org_id,
        count,
        used,
        reserved,
        limit,
    )


async def admit_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    count: int,
    *,
    api_key_id: str | None = None,
    allow_unattributed: bool = False,
) -> None:
    """Reject over-budget submissions (402/403).

    Takes no locks: concurrent admissions can briefly overshoot a cap, and
    the enforcement sweep cancels the overage.
    """
    mode = settings.quota_mode
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return
    enforce = mode == QuotaMode.ENFORCE

    if billed_user_id is None:
        if enforce and not allow_unattributed:
            raise Unattributed()
        # Unconditional in SHADOW, including on the retry path: the rollout
        # relies on scraping this to enumerate submissions with an unresolved
        # payer (see backend/README.md), and a retry has one just as a fresh
        # submit does.
        if mode == QuotaMode.SHADOW:
            _log_would_block(org_id, None, None, None, None, reason="unattributed")
        if allow_unattributed:
            await _check_unattributed_quota(session, org_id, count, enforce=enforce)
    else:
        await _check_user_quota(session, org_id, billed_user_id, count, enforce=enforce)

    if api_key_id is not None:
        await _check_api_key_quota(session, org_id, api_key_id, count, enforce=enforce)

    await _check_org_quota(session, org_id, billed_user_id, count, enforce=enforce)
