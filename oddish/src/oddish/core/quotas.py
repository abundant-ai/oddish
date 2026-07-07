from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import case, func, select, text
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


QUOTA_WINDOW = timedelta(hours=24)


def quota_window_start(now: datetime | None = None) -> datetime:
    """Return the start of the last 24 hours."""
    return (now or datetime.now(timezone.utc)) - QUOTA_WINDOW


def _settled_cost_predicates(org_id: str | None, period_start: datetime) -> list:
    return [
        TrialModel.org_id == org_id,
        TrialModel.finished_at > period_start,
    ]


def _settled_cost_expr():
    """Return the cost to count for one finished trial."""
    return func.coalesce(
        TrialModel.cost_usd,
        case(
            (
                TrialModel.started_at.isnot(None),
                float(settings.unpriced_trial_cost_usd),
            ),
            else_=0.0,
        ),
    )


async def sum_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    user_id: str,
    period_start: datetime,
) -> Decimal:
    return to_money_decimal(
        await session.scalar(
            select(func.coalesce(func.sum(_settled_cost_expr()), 0))
            .where(
                *_settled_cost_predicates(org_id, period_start),
                TrialModel.billed_user_id == user_id,
            )
            .execution_options(include_deleted=True)
        )
    )


async def sum_cost_usd_by_user(
    session: AsyncSession, org_id: str | None, period_start: datetime
) -> dict[str, Decimal]:
    rows = await session.execute(
        select(
            TrialModel.billed_user_id,
            func.coalesce(func.sum(_settled_cost_expr()), 0),
        )
        .where(
            *_settled_cost_predicates(org_id, period_start),
            TrialModel.billed_user_id.is_not(None),
        )
        .group_by(TrialModel.billed_user_id)
        .execution_options(include_deleted=True)
    )
    return {user_id: to_money_decimal(total) for user_id, total in rows.all()}


def _inflight_predicates(org_id: str | None, billed_user_id: str) -> list:
    return [
        TrialModel.org_id == org_id,
        TrialModel.billed_user_id == billed_user_id,
        TrialModel.finished_at.is_(None),
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
    ]


async def inflight_reserved_usd(
    session: AsyncSession, org_id: str | None, billed_user_id: str
) -> Decimal:
    """Return the reserved cost for running trials."""
    return to_money_decimal(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.greatest(
                            func.coalesce(TrialModel.cost_usd, 0),
                            float(settings.pending_trial_reservation_usd),
                        )
                    ),
                    0,
                )
            )
            .select_from(TrialModel)
            .where(*_inflight_predicates(org_id, billed_user_id))
        )
    )


_LIVE_BUMP = "revoked_at IS NULL AND deleted_at IS NULL AND expires_at > NOW()"


async def live_bump_total(
    session: AsyncSession, org_id: str | None, user_id: str
) -> tuple[Decimal, datetime | None]:
    total, max_expires_at = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(amount_usd), 0), MAX(expires_at) "
                "FROM quota_bumps "
                f"WHERE org_id = :org_id AND user_id = :user_id AND {_LIVE_BUMP}"
            ),
            {"org_id": org_id, "user_id": user_id},
        )
    ).one()
    return to_money_decimal(total), max_expires_at


async def live_bump_totals_by_user(
    session: AsyncSession, org_id: str | None
) -> dict[str, tuple[Decimal, datetime | None]]:
    rows = await session.execute(
        text(
            "SELECT user_id, COALESCE(SUM(amount_usd), 0), MAX(expires_at) "
            "FROM quota_bumps "
            f"WHERE org_id = :org_id AND {_LIVE_BUMP} "
            "GROUP BY user_id"
        ),
        {"org_id": org_id},
    )
    return {
        user_id: (to_money_decimal(total), max_expires_at)
        for user_id, total, max_expires_at in rows.all()
    }


async def get_base_limit(
    session: AsyncSession, org_id: str | None, user_id: str
) -> Decimal:
    override_limit_usd = await session.scalar(
        text(
            "SELECT limit_usd FROM quotas "
            "WHERE org_id = :org_id AND user_id = :user_id "
            "AND deleted_at IS NULL"
        ),
        {"org_id": org_id, "user_id": user_id},
    )
    if override_limit_usd is None:
        return settings.default_daily_quota_usd
    return Decimal(str(override_limit_usd))


async def get_effective_limit(
    session: AsyncSession, org_id: str | None, user_id: str
) -> Decimal:
    base_limit_usd = await get_base_limit(session, org_id, user_id)
    bump_total, _ = await live_bump_total(session, org_id, user_id)
    return base_limit_usd + bump_total
