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
    """Return the start of the last 24 hours (per-user rolling window)."""
    return (now or datetime.now(timezone.utc)) - QUOTA_WINDOW


def start_of_month_utc(now: datetime | None = None) -> datetime:
    """First day of the current UTC month at 00:00 (tz-aware).

    The org-wide cap is a CALENDAR-MONTH (UTC) budget that resets on the 1st,
    matching how billing periods land -- so the org admission sums and the org
    read endpoints anchor on this, while per-user paths keep the rolling 24h
    ``quota_window_start``.
    """
    return (now or datetime.now(timezone.utc)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def start_of_today_utc(now: datetime | None = None) -> datetime:
    """UTC midnight today (tz-aware).

    Used by the org daily-goal math (``GET /quotas/org``) to split
    month-to-date spend into "before today" vs "today". Not an enforcement
    window -- only the calendar month gates admissions.
    """
    return (now or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _settled_cost_predicates(
    org_id: str | None, period_start: datetime, *, inclusive_start: bool = False
) -> list:
    # Rolling windows are EXCLUSIVE (spend frees at exactly 24h after
    # finished_at); calendar-anchored windows are INCLUSIVE (the 1st at
    # 00:00:00 UTC belongs to the new month, midnight belongs to the new day).
    boundary = (
        TrialModel.finished_at >= period_start
        if inclusive_start
        else TrialModel.finished_at > period_start
    )
    return [TrialModel.org_id == org_id, boundary]


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


async def sum_org_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    period_start: datetime,
) -> Decimal:
    """Settled spend for the WHOLE org since ``period_start``: same unpriced
    floor as ``sum_cost_usd`` but with no ``billed_user_id`` filter, so
    unattributed (NULL-billed) trials DO count toward the org total. The org
    windows are calendar-anchored (month / day starts), so the boundary is
    INCLUSIVE, unlike the per-user rolling window. Bypasses the soft-delete
    filter (``include_deleted=True``): deleting is not a budget reset."""
    return to_money_decimal(
        await session.scalar(
            select(func.coalesce(func.sum(_settled_cost_expr()), 0))
            .where(
                *_settled_cost_predicates(org_id, period_start, inclusive_start=True)
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


def _org_inflight_predicates(org_id: str | None) -> list:
    return [
        TrialModel.org_id == org_id,
        TrialModel.finished_at.is_(None),
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
    ]


async def org_inflight_reserved_usd(
    session: AsyncSession, org_id: str | None
) -> Decimal:
    """Org-wide in-flight reservation: same per-trial expression as
    ``inflight_reserved_usd`` (accumulated cost floored at the pending
    reservation) but summed across every payer in the org, with no
    ``billed_user_id`` predicate. Not time-bound -- an in-flight trial reserves
    regardless of when it started."""
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
            .where(*_org_inflight_predicates(org_id))
        )
    )


async def get_effective_org_limit(
    session: AsyncSession, org_id: str | None
) -> Decimal | None:
    """Effective org-wide monthly cap: a live override row wins, else the
    configured default, else ``None`` (= no org cap). Read via raw ``text()``
    SQL so oddish core never imports the backend-only ``OrgQuotaModel``."""
    override_limit_usd = await session.scalar(
        text(
            "SELECT limit_usd FROM org_quotas "
            "WHERE org_id = :org_id AND deleted_at IS NULL"
        ),
        {"org_id": org_id},
    )
    if override_limit_usd is not None:
        return Decimal(str(override_limit_usd))
    return settings.default_org_monthly_quota_usd


async def get_effective_limit(
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
