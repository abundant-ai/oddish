from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TrialModel


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
    return Decimal(str(settled_cost_total or 0)).quantize(Decimal("0.0001"))
