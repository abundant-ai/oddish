from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import TrialModel, TrialStatus

MONEY_QUANTUM = Decimal("0.0001")

_INFLIGHT_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)


def to_money_decimal(raw_amount) -> Decimal:
    return Decimal(str(raw_amount or 0)).quantize(MONEY_QUANTUM)


def start_of_today_utc(now: datetime | None = None) -> datetime:
    current_instant = now or datetime.now(timezone.utc)
    return current_instant.replace(hour=0, minute=0, second=0, microsecond=0)


async def sum_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    user_id: str,
    period_start: datetime,
) -> Decimal:
    settled_cost_total = await session.scalar(
        select(func.coalesce(func.sum(TrialModel.cost_usd), 0)).where(
            TrialModel.org_id == org_id,
            TrialModel.billed_user_id == user_id,
            TrialModel.finished_at >= period_start,
            TrialModel.deleted_at.is_(None),
        )
    )
    return to_money_decimal(settled_cost_total)


async def inflight_count(
    session: AsyncSession, org_id: str | None, billed_user_id: str
) -> int:
    active_trial_total = await session.scalar(
        select(func.count())
        .select_from(TrialModel)
        .where(
            TrialModel.org_id == org_id,
            TrialModel.billed_user_id == billed_user_id,
            TrialModel.finished_at.is_(None),
            TrialModel.deleted_at.is_(None),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
        )
    )
    return int(active_trial_total or 0)


async def get_effective_limit(
    session: AsyncSession, org_id: str | None, user_id: str
) -> Decimal:
    override_limit_usd = await session.scalar(
        text(
            "SELECT limit_usd FROM quotas "
            "WHERE org_id = :org_id AND user_id = :user_id AND deleted_at IS NULL"
        ),
        {"org_id": org_id, "user_id": user_id},
    )
    if override_limit_usd is not None:
        return Decimal(str(override_limit_usd))
    return settings.default_daily_quota_usd
