from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reported_usd,
    org_inflight_reported_usd,
    quota_pause_limit_usd,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_org_cost_usd,
)


async def quota_pause_requested(
    session: AsyncSession,
    *,
    org_id: str | None,
    billed_user_id: str | None,
) -> bool:
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        return False

    org_limit = quota_pause_limit_usd(await get_effective_org_limit(session, org_id))
    if org_limit is not None:
        org_used = await sum_org_cost_usd(session, org_id, start_of_month_utc())
        org_inflight = await org_inflight_reported_usd(session, org_id)
        if org_used + org_inflight >= org_limit:
            return True

    if billed_user_id is None:
        return False
    user_limit = quota_pause_limit_usd(
        await get_effective_limit(session, org_id, billed_user_id)
    )
    if user_limit is None:
        return False
    user_used = await sum_cost_usd(
        session, org_id, billed_user_id, quota_window_start()
    )
    user_inflight = await inflight_reported_usd(session, org_id, billed_user_id)
    return user_used + user_inflight >= user_limit
