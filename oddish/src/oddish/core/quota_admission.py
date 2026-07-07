from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import QuotaMode, settings
from oddish.core.quotas import (
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    org_inflight_reserved_usd,
    quota_window_start,
    start_of_month_utc,
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


def reject_unattributed_if_enforced(
    org_id: str | None, billed_user_id: str | None
) -> None:
    """Raise ``Unattributed`` (403) for a doomed submission BEFORE any locks.

    Under ENFORCE an unattributed submission can never be admitted, so callers
    that take the org/payer locks up front (the sweep cores) must reject it
    first -- otherwise a request that cannot succeed holds the org-wide
    advisory lock through Harbor/S3 resolution and blocks every other
    admission in the org. Mirrors the identical check inside ``admit_trials``.
    """
    if (
        settings.quota_mode == QuotaMode.ENFORCE
        and org_id is not None
        and billed_user_id is None
    ):
        raise Unattributed()


async def acquire_org_lock(
    session: AsyncSession, org_id: str | None
) -> Decimal | None:
    """Serialize org-wide quota admissions; released at commit.

    Only under quota_mode == ENFORCE and only when an org cap is actually
    configured (a live override row or the configured default): shadow/off must
    not serialize, and an org with no cap must not serialize an entire org for a
    disabled check.

    Returns the effective org limit as re-read UNDER the lock, or ``None`` when
    no lock was taken (off/shadow/no org/capless). The pre-lock read only
    decides WHETHER to lock; a concurrent ``PUT /quotas/org`` between that read
    and the lock could otherwise leave the enforcement check comparing against
    a stale, unserialized value -- so ENFORCE callers must use the returned
    limit rather than re-reading without the lock. The one residual race is
    capless-at-lock-time: an admission that read ``None`` here skips the org
    check even if a cap lands a moment later (bounded to the flip instant; the
    next admission sees the cap).

    LOCK ORDER: the org lock is taken BEFORE the payer lock (org -> payer -> row
    locks); see acquire_payer_lock. Re-acquiring a held xact advisory lock is a
    no-op.
    """
    if settings.quota_mode != QuotaMode.ENFORCE or org_id is None:
        return None
    if await get_effective_org_limit(session, org_id) is None:
        return None
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"quota-org:{org_id}"},
    )
    return await get_effective_org_limit(session, org_id)


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
    *,
    allow_unattributed: bool = False,
) -> None:
    mode = settings.quota_mode
    # OSS/self-hosted single-tenant (no org -> no payer) never enforces, even when
    # a billed_user_id is present.
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return

    # Under ENFORCE an unattributed submission is rejected outright (as today),
    # before taking any lock. Under shadow it falls through so the org-wide check
    # below still sees the NULL-billed spend. ``allow_unattributed`` is the
    # legacy-retry carve-out: a retry of a pre-attribution (NULL-billed) trial
    # is exempt from the linkage requirement but must still pass the ORG-wide
    # cap below -- NULL-billed spend counts toward the org total, so skipping
    # admission entirely would let an over-cap org keep retrying forever.
    if billed_user_id is None and mode == QuotaMode.ENFORCE and not allow_unattributed:
        raise Unattributed()

    # LOCK ORDER: org lock BEFORE payer lock. No-op in shadow/off or when no org
    # cap is configured. Under ENFORCE the returned limit was re-read while
    # HOLDING the lock -- the org check below must use it (not re-read) so the
    # enforced value can't race a concurrent PUT /quotas/org.
    locked_org_limit_usd = await acquire_org_lock(session, org_id)

    if billed_user_id is None:
        # Shadow-only signal: under ENFORCE this point is only reachable via
        # the legacy-retry carve-out, which is permitted, not would-be-blocked.
        if mode == QuotaMode.SHADOW:
            _log_would_block(org_id, None, None, None, None, reason="unattributed")
    else:
        await acquire_payer_lock(session, org_id, billed_user_id)

        # Per-user cap keeps the rolling-24h window (unchanged behavior).
        effective_limit_usd = await get_effective_limit(
            session, org_id, billed_user_id
        )
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

    # Org-wide aggregate cap: CALENDAR-MONTH (UTC) window. Runs for attributed
    # AND unattributed submissions (unattributed NULL-billed spend still counts
    # toward the org total). ENFORCE uses the under-lock limit from
    # acquire_org_lock; shadow is log-only and tolerates an unserialized read.
    org_limit_usd = (
        locked_org_limit_usd
        if mode == QuotaMode.ENFORCE
        else await get_effective_org_limit(session, org_id)
    )
    if org_limit_usd is not None:
        org_used_usd = await sum_org_cost_usd(session, org_id, start_of_month_utc())
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
