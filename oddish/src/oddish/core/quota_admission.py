from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    inflight_reserved_usd,
    quota_window_start,
    sum_cost_usd,
)

logger = logging.getLogger(__name__)


class QuotaExceeded(HTTPException):
    def __init__(self, used_usd, reserved_usd, limit_usd) -> None:
        super().__init__(
            status_code=402,
            detail={
                "message": (
                    f"Over your budget: used ${float(used_usd):.2f} + "
                    f"${float(reserved_usd):.2f} reserved of "
                    f"${float(limit_usd):.2f} (rolling 24h). Spend ages out "
                    "24h after a run finishes; ask an org admin to raise "
                    "your quota."
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


async def acquire_payer_lock(
    session: AsyncSession, org_id: str | None, billed_user_id: str | None
) -> None:
    """Serialize same-payer transactions; released at commit.

    Only under quota_mode == ENFORCE: shadow admissions are read-only logging
    and tolerate races, so the dark feature must not serialize submissions.
    LOCK ORDER: this must be the FIRST lock a quota-gated transaction takes --
    before any row lock (task refresh, trial CAS, worker_jobs) or concurrent
    submit/retry paths deadlock ABBA. The sweep cores acquire it up front;
    retry relies on admit_trials acquiring it before its first row lock.
    Re-acquiring a held xact advisory lock is a no-op.
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

    if billed_user_id is None:
        if mode == QuotaMode.ENFORCE:
            raise Unattributed()
        _log_would_block(org_id, None, None, None, None, reason="unattributed")
        return

    await acquire_payer_lock(session, org_id, billed_user_id)

    effective_limit_usd = await get_effective_limit(session, org_id, billed_user_id)
    used_usd = await sum_cost_usd(
        session, org_id, billed_user_id, quota_window_start()
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
