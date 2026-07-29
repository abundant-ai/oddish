from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.core.cost_basis import (
    first_party_spend_filter,
    not_excluded_llm_key_filter,
    settled_cost_columns,
    settled_cost_from_row,
    sum_settled_cost,
)
from oddish.db import AnalysisCostModel, ModalCostSpanModel, TrialModel, TrialStatus

logger = logging.getLogger(__name__)

MONEY_QUANTUM = Decimal("0.0001")

_INFLIGHT_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)

_LIVE_BUMP = "revoked_at IS NULL AND deleted_at IS NULL AND expires_at > NOW()"


def to_money_decimal(raw_amount) -> Decimal:
    return Decimal(str(raw_amount or 0)).quantize(MONEY_QUANTUM)


QUOTA_WINDOW = timedelta(hours=24)


def quota_window_start(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - QUOTA_WINDOW


def start_of_month_utc(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


async def acquire_quota_locks(
    session: AsyncSession,
    org_id: str,
    billed_user_id: str | None,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"quota:org:{org_id}"},
    )
    if billed_user_id is not None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"quota:user:{org_id}:{billed_user_id}"},
        )


def start_of_today_utc(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _settled_cost_predicates(
    org_id: str | None, period_start: datetime, *, inclusive_start: bool = False
) -> list:
    boundary = (
        TrialModel.finished_at >= period_start
        if inclusive_start
        else TrialModel.finished_at > period_start
    )
    return [TrialModel.org_id == org_id, boundary, first_party_spend_filter()]


async def _sum_settled_cost_usd(session: AsyncSession, predicates: list) -> Decimal:
    # Grouped by model so the Python token estimate in ``cost_basis`` can price
    # unpriced trials the same way the cost dashboard does (see cost_basis docs).
    rows = await session.execute(
        select(TrialModel.model.label("model"), *settled_cost_columns())
        .where(*predicates)
        .group_by(TrialModel.model)
        .execution_options(include_deleted=True)
    )
    return to_money_decimal(sum_settled_cost(rows.all()))


def _timestamp_in_period(column, period_start: datetime, *, inclusive_start: bool):
    return column >= period_start if inclusive_start else column > period_start


def _quota_counts_analysis_and_compute() -> bool:
    return settings.quota_counts_analysis_and_compute


async def _sum_analysis_and_compute_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    period_start: datetime,
    *,
    billed_user_id: str | None = None,
    filter_user: bool = False,
    inclusive_start: bool = False,
) -> Decimal:
    analysis_predicates = [
        AnalysisCostModel.org_id == org_id,
        AnalysisCostModel.deleted_at.is_(None),
        _timestamp_in_period(
            AnalysisCostModel.created_at, period_start, inclusive_start=inclusive_start
        ),
    ]
    compute_predicates = [
        ModalCostSpanModel.org_id == org_id,
        ModalCostSpanModel.deleted_at.is_(None),
        ModalCostSpanModel.finished_at.is_not(None),
        ModalCostSpanModel.cost_usd.is_not(None),
        _timestamp_in_period(
            ModalCostSpanModel.finished_at,
            period_start,
            inclusive_start=inclusive_start,
        ),
    ]
    if filter_user:
        analysis_predicates.append(AnalysisCostModel.billed_user_id == billed_user_id)
        compute_predicates.append(ModalCostSpanModel.billed_user_id == billed_user_id)
    analysis = await session.scalar(
        select(func.coalesce(func.sum(AnalysisCostModel.cost_usd), 0)).where(
            *analysis_predicates
        )
    )
    compute = await session.scalar(
        select(func.coalesce(func.sum(ModalCostSpanModel.cost_usd), 0)).where(
            *compute_predicates
        )
    )
    return to_money_decimal(analysis) + to_money_decimal(compute)


async def _analysis_and_compute_cost_by_user(
    session: AsyncSession,
    org_id: str | None,
    period_start: datetime,
    *,
    inclusive_start: bool = False,
) -> dict[str, float]:
    totals: dict[str, float] = {}
    analysis_rows = await session.execute(
        select(
            AnalysisCostModel.billed_user_id,
            func.coalesce(func.sum(AnalysisCostModel.cost_usd), 0.0),
        )
        .where(
            AnalysisCostModel.org_id == org_id,
            AnalysisCostModel.billed_user_id.is_not(None),
            AnalysisCostModel.deleted_at.is_(None),
            _timestamp_in_period(
                AnalysisCostModel.created_at,
                period_start,
                inclusive_start=inclusive_start,
            ),
        )
        .group_by(AnalysisCostModel.billed_user_id)
    )
    for user_id, cost_usd in analysis_rows.all():
        totals[user_id] = totals.get(user_id, 0.0) + float(cost_usd or 0)
    compute_rows = await session.execute(
        select(
            ModalCostSpanModel.billed_user_id,
            func.coalesce(func.sum(ModalCostSpanModel.cost_usd), 0.0),
        )
        .where(
            ModalCostSpanModel.org_id == org_id,
            ModalCostSpanModel.billed_user_id.is_not(None),
            ModalCostSpanModel.deleted_at.is_(None),
            ModalCostSpanModel.finished_at.is_not(None),
            ModalCostSpanModel.cost_usd.is_not(None),
            _timestamp_in_period(
                ModalCostSpanModel.finished_at,
                period_start,
                inclusive_start=inclusive_start,
            ),
        )
        .group_by(ModalCostSpanModel.billed_user_id)
    )
    for user_id, cost_usd in compute_rows.all():
        totals[user_id] = totals.get(user_id, 0.0) + float(cost_usd or 0)
    return totals


async def _analysis_and_compute_cost_by_org_user(
    session: AsyncSession,
    period_start: datetime,
    *,
    inclusive_start: bool = False,
) -> dict[tuple[str | None, str], float]:
    totals: dict[tuple[str | None, str], float] = {}
    analysis_rows = await session.execute(
        select(
            AnalysisCostModel.org_id,
            AnalysisCostModel.billed_user_id,
            func.coalesce(func.sum(AnalysisCostModel.cost_usd), 0.0),
        )
        .where(
            AnalysisCostModel.billed_user_id.is_not(None),
            AnalysisCostModel.deleted_at.is_(None),
            _timestamp_in_period(
                AnalysisCostModel.created_at,
                period_start,
                inclusive_start=inclusive_start,
            ),
        )
        .group_by(AnalysisCostModel.org_id, AnalysisCostModel.billed_user_id)
    )
    for org_id, user_id, cost_usd in analysis_rows.all():
        key = (org_id, user_id)
        totals[key] = totals.get(key, 0.0) + float(cost_usd or 0)
    compute_rows = await session.execute(
        select(
            ModalCostSpanModel.org_id,
            ModalCostSpanModel.billed_user_id,
            func.coalesce(func.sum(ModalCostSpanModel.cost_usd), 0.0),
        )
        .where(
            ModalCostSpanModel.billed_user_id.is_not(None),
            ModalCostSpanModel.deleted_at.is_(None),
            ModalCostSpanModel.finished_at.is_not(None),
            ModalCostSpanModel.cost_usd.is_not(None),
            _timestamp_in_period(
                ModalCostSpanModel.finished_at,
                period_start,
                inclusive_start=inclusive_start,
            ),
        )
        .group_by(ModalCostSpanModel.org_id, ModalCostSpanModel.billed_user_id)
    )
    for org_id, user_id, cost_usd in compute_rows.all():
        key = (org_id, user_id)
        totals[key] = totals.get(key, 0.0) + float(cost_usd or 0)
    return totals


async def sum_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    user_id: str,
    period_start: datetime,
) -> Decimal:
    total = await _sum_settled_cost_usd(
        session,
        [
            *_settled_cost_predicates(org_id, period_start),
            TrialModel.billed_user_id == user_id,
        ],
    )
    if _quota_counts_analysis_and_compute():
        total += await _sum_analysis_and_compute_cost_usd(
            session,
            org_id,
            period_start,
            billed_user_id=user_id,
            filter_user=True,
            inclusive_start=False,
        )
    return total


async def sum_org_cost_usd(
    session: AsyncSession,
    org_id: str | None,
    period_start: datetime,
) -> Decimal:
    total = await _sum_settled_cost_usd(
        session, _settled_cost_predicates(org_id, period_start, inclusive_start=True)
    )
    if _quota_counts_analysis_and_compute():
        total += await _sum_analysis_and_compute_cost_usd(
            session, org_id, period_start, inclusive_start=True
        )
    return total


async def sum_cost_usd_by_user(
    session: AsyncSession, org_id: str | None, period_start: datetime
) -> dict[str, Decimal]:
    rows = await session.execute(
        select(
            TrialModel.billed_user_id.label("billed_user_id"),
            TrialModel.model.label("model"),
            *settled_cost_columns(),
        )
        .where(
            *_settled_cost_predicates(org_id, period_start),
            TrialModel.billed_user_id.is_not(None),
        )
        .group_by(TrialModel.billed_user_id, TrialModel.model)
        .execution_options(include_deleted=True)
    )
    totals: dict[str, float] = {}
    for row in rows.all():
        totals[row.billed_user_id] = totals.get(
            row.billed_user_id, 0.0
        ) + settled_cost_from_row(row)
    if _quota_counts_analysis_and_compute():
        for user_id, amount in (
            await _analysis_and_compute_cost_by_user(
                session, org_id, period_start, inclusive_start=False
            )
        ).items():
            totals[user_id] = totals.get(user_id, 0.0) + amount
    return {user_id: to_money_decimal(total) for user_id, total in totals.items()}


async def sum_cost_usd_by_org_user_all_orgs(
    session: AsyncSession, period_start: datetime
) -> dict[tuple[str | None, str], Decimal]:
    """Settled 24h spend keyed the same way admission checks spend."""
    rows = await session.execute(
        select(
            TrialModel.org_id.label("org_id"),
            TrialModel.billed_user_id.label("billed_user_id"),
            TrialModel.model.label("model"),
            *settled_cost_columns(),
        )
        .where(
            TrialModel.finished_at > period_start,
            TrialModel.billed_user_id.is_not(None),
            first_party_spend_filter(),
        )
        .group_by(TrialModel.org_id, TrialModel.billed_user_id, TrialModel.model)
        .execution_options(include_deleted=True)
    )
    totals: dict[tuple[str | None, str], float] = {}
    for row in rows.all():
        key = (row.org_id, row.billed_user_id)
        totals[key] = totals.get(key, 0.0) + settled_cost_from_row(row)
    if _quota_counts_analysis_and_compute():
        for key, amount in (
            await _analysis_and_compute_cost_by_org_user(
                session, period_start, inclusive_start=False
            )
        ).items():
            totals[key] = totals.get(key, 0.0) + amount
    return {key: to_money_decimal(total) for key, total in totals.items()}


async def _base_limits_by_org_user_all_orgs(
    session: AsyncSession,
) -> dict[tuple[str | None, str], Decimal]:
    rows = await session.execute(
        text("SELECT org_id, user_id, limit_usd FROM quotas WHERE deleted_at IS NULL")
    )
    return {
        (org_id, user_id): to_money_decimal(limit_usd)
        for org_id, user_id, limit_usd in rows.all()
    }


async def effective_limits_by_org_user_all_orgs(
    session: AsyncSession,
) -> dict[tuple[str | None, str], Decimal]:
    """Live quota limits (base + live bumps), keyed as admission checks limits.

    Degrades to base-only if ``quota_bumps`` is absent: it is the last of the
    three quota migrations, so "quotas exists, quota_bumps does not" is the
    realistic deploy-before-migrate window, and this read sits behind the whole
    admin cost dashboard. The savepoint keeps the caller's transaction usable.
    """
    try:
        async with session.begin_nested():
            return await _bump_aware_limits_by_org_user_all_orgs(session)
    except ProgrammingError:
        logger.warning(
            "quota bumps unavailable (schema not migrated yet); reporting "
            "base-only limits",
            exc_info=True,
        )
        return await _base_limits_by_org_user_all_orgs(session)


async def _bump_aware_limits_by_org_user_all_orgs(
    session: AsyncSession,
) -> dict[tuple[str | None, str], Decimal]:
    rows = await session.execute(
        text(
            "SELECT keys.org_id, keys.user_id, "
            "COALESCE(q.limit_usd, :default_limit) + COALESCE(b.bump_total, 0) "
            "AS limit_usd "
            "FROM ("
            "  SELECT org_id, user_id FROM quotas WHERE deleted_at IS NULL "
            "  UNION "
            f"  SELECT org_id, user_id FROM quota_bumps WHERE {_LIVE_BUMP}"
            ") AS keys "
            "LEFT JOIN quotas q ON q.org_id = keys.org_id AND q.user_id = keys.user_id "
            "AND q.deleted_at IS NULL "
            "LEFT JOIN ("
            "  SELECT org_id, user_id, SUM(amount_usd) AS bump_total "
            f"  FROM quota_bumps WHERE {_LIVE_BUMP} "
            "  GROUP BY org_id, user_id"
            ") b ON b.org_id = keys.org_id AND b.user_id = keys.user_id"
        ),
        {"default_limit": settings.default_daily_quota_usd},
    )
    out: dict[tuple[str | None, str], Decimal] = {}
    for org_id, user_id, limit_usd in rows.all():
        out[(org_id, user_id)] = to_money_decimal(limit_usd)
    return out


def _inflight_predicates(org_id: str | None, billed_user_id: str) -> list:
    # ``not_excluded_llm_key_filter``: a RETRYING attempt already carries its
    # settlement stamp while finished_at is still NULL; spend the settled sums
    # will drop must not keep reserving against the cap either.
    return [
        TrialModel.org_id == org_id,
        TrialModel.billed_user_id == billed_user_id,
        TrialModel.finished_at.is_(None),
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
        not_excluded_llm_key_filter(),
    ]


def _org_inflight_predicates(org_id: str | None) -> list:
    return [
        TrialModel.org_id == org_id,
        TrialModel.finished_at.is_(None),
        TrialModel.deleted_at.is_(None),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
        not_excluded_llm_key_filter(),
    ]


async def _sum_inflight_reserved_usd(
    session: AsyncSession, predicates: list
) -> Decimal:
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
            .where(*predicates)
        )
    )


async def inflight_trial_count_by_org_user_all_orgs(
    session: AsyncSession,
) -> dict[tuple[str | None, str], int]:
    """In-flight trial count keyed by the quota admission predicate."""
    rows = await session.execute(
        select(TrialModel.org_id, TrialModel.billed_user_id, func.count(TrialModel.id))
        .where(
            TrialModel.billed_user_id.is_not(None),
            TrialModel.finished_at.is_(None),
            TrialModel.deleted_at.is_(None),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(_INFLIGHT_TRIAL_STATUSES),
            not_excluded_llm_key_filter(),
        )
        .group_by(TrialModel.org_id, TrialModel.billed_user_id)
    )
    out: dict[tuple[str | None, str], int] = {}
    for org_id, user_id, count in rows.all():
        out[(org_id, user_id)] = int(count or 0)
    return out


async def inflight_reserved_usd(
    session: AsyncSession, org_id: str | None, billed_user_id: str
) -> Decimal:
    return await _sum_inflight_reserved_usd(
        session, _inflight_predicates(org_id, billed_user_id)
    )


async def org_inflight_reserved_usd(
    session: AsyncSession, org_id: str | None
) -> Decimal:
    return await _sum_inflight_reserved_usd(session, _org_inflight_predicates(org_id))


# An org's UNATTRIBUTED spend, pooled. A trial whose payer could not be resolved
# has no per-user cap to charge, so admission would otherwise wave it through on
# the retry path; these two give that pool its own ceiling. Trial-only on
# purpose: retrying a trial re-runs the trial, so the analyzer/compute basis in
# ``_sum_analysis_and_compute_cost_usd`` is not what the retry is spending.
async def unattributed_cost_usd(
    session: AsyncSession, org_id: str | None, period_start: datetime
) -> Decimal:
    return await _sum_settled_cost_usd(
        session,
        [
            *_settled_cost_predicates(org_id, period_start),
            TrialModel.billed_user_id.is_(None),
        ],
    )


async def unattributed_inflight_reserved_usd(
    session: AsyncSession, org_id: str | None
) -> Decimal:
    return await _sum_inflight_reserved_usd(
        session,
        [*_org_inflight_predicates(org_id), TrialModel.billed_user_id.is_(None)],
    )


async def get_effective_org_limit(
    session: AsyncSession, org_id: str | None
) -> Decimal | None:
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
