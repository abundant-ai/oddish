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
    """Start of the rolling 24-hour quota window.

    The budget window slides continuously (now - 24h) instead of resetting at
    UTC calendar midnight, so spend "ages out" exactly 24h after it settled and
    there is no reset moment to burst around (2x the cap by straddling
    midnight).
    """
    return (now or datetime.now(timezone.utc)) - QUOTA_WINDOW


# Settled spend counts even after a trial/experiment is soft-deleted (deleting
# is not a budget reset), so the sums bypass the deleted_at auto-filter.
def _settled_cost_predicates(org_id: str | None, period_start: datetime) -> list:
    return [
        TrialModel.org_id == org_id,
        TrialModel.finished_at >= period_start,
    ]


def _settled_cost_expr():
    """Per-trial settled cost for the budget SUM: an unpriced (NULL cost_usd)
    finished trial counts as ``unpriced_trial_cost_usd`` so it is never billed
    as free. A genuinely-$0 row (cost_usd = 0) is left as $0. The floor is gated
    on ``started_at IS NOT NULL``: a never-started trial (cancelled while
    PENDING/QUEUED, so it got finished_at but never claimed a worker) genuinely
    cost $0 and can't be part of the start-then-cancel bypass, so it counts as $0."""
    return func.coalesce(
        TrialModel.cost_usd,
        case(
            (TrialModel.started_at.isnot(None), float(settings.unpriced_trial_cost_usd)),
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
    """Reservation for in-flight trials: each stands in at its accumulated
    cost so far (a $3 RETRYING attempt reserves $3), floored at the pending
    reservation."""
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


async def get_effective_limit(
    session: AsyncSession, org_id: str | None, user_id: str
) -> Decimal:
    # ``deleted_at IS NULL`` matches the soft-delete convention + the S5 rollout
    # docs. Override removal is currently a hard DELETE (orgs.py), so there are
    # no soft-deleted rows today, but this keeps a removed override from ever
    # enforcing an old limit if a soft-delete path is later introduced.
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
