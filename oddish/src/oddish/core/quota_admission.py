from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    start_of_today_utc,
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
                    f"Over your daily budget: used ${float(used_usd):.2f} + "
                    f"${float(reserved_usd):.2f} reserved of "
                    f"${float(limit_usd):.2f} (daily). Ask an org admin to "
                    "raise your quota."
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
                    f"Your organization is over its daily budget: used "
                    f"${float(used_usd):.2f} + ${float(reserved_usd):.2f} "
                    f"reserved of ${float(limit_usd):.2f} (daily). Ask an org "
                    "admin to raise the org quota."
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


async def acquire_org_lock(session: AsyncSession, org_id: str | None) -> None:
    """Serialize org-wide quota admissions; released at commit.

    Only under quota_mode == ENFORCE and only when an org cap is actually
    configured (a live override row or the configured default): shadow/off must
    not serialize, and an org with no cap must not serialize an entire org for a
    disabled check.
    LOCK ORDER: the org lock is taken BEFORE the payer lock (org -> payer -> row
    locks); see acquire_payer_lock. Re-acquiring a held xact advisory lock is a
    no-op.
    """
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        return
    if await get_effective_org_limit(session, org_id) is None:
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"quota-org:{org_id}"},
    )


async def acquire_payer_lock(
    session: AsyncSession, org_id: str | None, billed_user_id: str | None
) -> None:
    """Serialize same-payer transactions; released at commit.

    Only under quota_mode == ENFORCE: shadow admissions are read-only logging
    and tolerate races, so the dark feature must not serialize submissions.
    LOCK ORDER: org lock (acquire_org_lock) -> payer lock (this) -> row locks
    (task refresh, trial CAS, worker_jobs). The payer lock must precede any row
    lock or concurrent submit/retry paths deadlock ABBA, and the org lock must
    precede the payer lock for the same reason across the org-wide cap. The
    sweep cores acquire the org lock then the payer lock up front; retry relies
    on admit_trials acquiring both before its first row lock. Re-acquiring a
    held xact advisory lock is a no-op.
    """
    if (
        settings.quota_mode != QuotaMode.ENFORCE
        or org_id is None
        or billed_user_id is None
    ):
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"quota:{org_id}:{billed_user_id}"},
    )


async def admit_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    count: int,
) -> None:
    mode = settings.quota_mode
    # OSS/self-hosted single-tenant (no org -> no payer) never enforces, even when
    # a billed_user_id is present.
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return

    # Under ENFORCE an unattributed submission is rejected outright (as today),
    # before taking any lock. Under shadow it falls through so the org-wide check
    # below still sees the NULL-billed spend.
    if billed_user_id is None and mode == QuotaMode.ENFORCE:
        raise Unattributed()

    # LOCK ORDER: org lock BEFORE payer lock. No-op in shadow/off or when no org
    # cap is configured.
    await acquire_org_lock(session, org_id)

    if billed_user_id is None:
        _log_would_block(org_id, None, None, None, None, reason="unattributed")
    else:
        await acquire_payer_lock(session, org_id, billed_user_id)

        effective_limit_usd = await get_effective_limit(
            session, org_id, billed_user_id
        )
        used_usd = await sum_cost_usd(
            session, org_id, billed_user_id, start_of_today_utc()
        )
        reserved_usd = (
            await inflight_reserved_usd(session, org_id, billed_user_id)
            + count * settings.pending_trial_reservation_usd
        )

        if used_usd + reserved_usd >= effective_limit_usd:
            if mode == QuotaMode.ENFORCE:
                raise QuotaExceeded(used_usd, reserved_usd, effective_limit_usd)
            _log_would_block(
                org_id,
                billed_user_id,
                used_usd,
                reserved_usd,
                effective_limit_usd,
                reason="over_budget",
            )

    # Org-wide aggregate cap: runs for attributed AND unattributed submissions
    # (unattributed NULL-billed spend still counts toward the org total).
    org_limit_usd = await get_effective_org_limit(session, org_id)
    if org_limit_usd is not None:
        org_used_usd = await sum_org_cost_usd(session, org_id, start_of_today_utc())
        org_reserved_usd = (
            await org_inflight_reserved_usd(session, org_id)
            + count * settings.pending_trial_reservation_usd
        )
        if org_used_usd + org_reserved_usd >= org_limit_usd:
            if mode == QuotaMode.ENFORCE:
                raise OrgQuotaExceeded(org_used_usd, org_reserved_usd, org_limit_usd)
            _log_would_block(
                org_id,
                billed_user_id,
                org_used_usd,
                org_reserved_usd,
                org_limit_usd,
                reason="org_over_budget",
            )
