from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import or_, select

from auth import provisioning
from auth.provisioning import (
    _mark_github_id_checked,
    _marker_is_fresh,
    _set_github_id_if_absent,
    fetch_github_identity_from_clerk,
    github_id_recheck_cutoff_iso,
)
from models import UserModel
from oddish.db import get_session

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
DEFAULT_CONCURRENCY = 4
DEFAULT_DELAY_SECONDS = 0.2
DEFAULT_TIME_BUDGET_SECONDS = 60.0
# Per-run cap on individual failure warnings so a full-failure run (Clerk down)
# does not emit one warning line per user (~200/run at the reconcile cadence).
MAX_FAILURE_WARNINGS = 3
# Minimum batch size at which an all-failed batch is treated as a persistent
# outage worth aborting the run for. A tiny tail batch of one flaky user must
# not trip the early stop.
ALL_FAILED_ABORT_FLOOR = 10


@dataclass
class BackfillSummary:
    scanned: int = 0
    set: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "github_id_backfill_scanned": self.scanned,
            "github_id_backfill_set": self.set,
            "github_id_backfill_skipped": self.skipped,
            "github_id_backfill_failed": self.failed,
        }


async def backfill_github_id(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_users: int | None = None,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> BackfillSummary:
    """Best-effort backfill of ``users.github_id`` from Clerk.

    Runs in three phases per batch so **no DB session is ever open across the
    Clerk HTTP fetches** (see AGENTS.md "Worker Runtime Invariants"): select
    candidate ids in a short session (A), run the throttled Clerk fetches with
    no session open (B), then re-load the rows and apply writes in a short
    session (C). Phase C re-checks preconditions because a row can change
    between phases.
    """
    summary = BackfillSummary()

    # Read the secret off the module at call time so tests can monkeypatch it.
    if not provisioning.CLERK_SECRET_KEY:
        logger.debug(
            "github_id backfill: CLERK_SECRET_KEY unset; skipping (no Clerk access)"
        )
        return summary

    start = time.monotonic()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    after_id: str | None = None
    cutoff_iso = github_id_recheck_cutoff_iso()
    failure_warnings_logged = 0

    async def _fetch(clerk_user_id: str):
        async with semaphore:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            return await fetch_github_identity_from_clerk(clerk_user_id)

    while max_users is None or summary.scanned < max_users:
        if time.monotonic() - start >= time_budget_seconds:
            break
        remaining = (
            batch_size if max_users is None else min(batch_size, max_users - summary.scanned)
        )
        if remaining <= 0:
            break

        # Phase A (short session): materialize (id, clerk_user_id) tuples, then
        # close the session before any HTTP happens.
        async with get_session() as session:
            stmt = (
                select(UserModel)
                .where(UserModel.clerk_user_id.isnot(None))
                .where(UserModel.github_id.is_(None))
                .where(UserModel.is_active == True)  # noqa: E712
                .where(
                    or_(
                        UserModel.attribution_cache.is_(None),
                        ~UserModel.attribution_cache.has_key("github_id_checked"),
                        # String < is chronological only because every marker is
                        # UTC-normalized fixed-width ISO via _github_id_checked_iso.
                        UserModel.attribution_cache["github_id_checked"].astext
                        < cutoff_iso,
                    )
                )
            )
            if after_id is not None:
                stmt = stmt.where(UserModel.id > after_id)
            result = await session.execute(
                stmt.order_by(UserModel.id.asc()).limit(remaining)
            )
            candidates = [
                (user.id, user.clerk_user_id) for user in result.scalars().all()
            ]
        if not candidates:
            break
        # Advance over skipped rows so keyset pagination terminates.
        after_id = candidates[-1][0]

        # Phase B (no session open): throttled Clerk fetches, hard-capped by the
        # remaining budget. Without this, one slow batch (batch_size fetches at
        # the 10s httpx timeout over `concurrency` lanes) could run minutes past
        # the between-batch budget check. A timeout cancels the in-flight
        # fetches (no session is open, so cancellation is clean), drops the
        # batch's results, and ends the run; unstamped rows retry next run.
        remaining_budget = time_budget_seconds - (time.monotonic() - start)
        if remaining_budget <= 0:
            break
        try:
            fetched = await asyncio.wait_for(
                asyncio.gather(
                    *(_fetch(clerk_user_id) for _, clerk_user_id in candidates),
                    return_exceptions=True,
                ),
                timeout=remaining_budget,
            )
        except TimeoutError:
            logger.warning(
                "github_id backfill: time budget (%.0fs) exceeded mid-batch; "
                "stopping run early (batch of %s dropped, will retry next run)",
                time_budget_seconds,
                len(candidates),
            )
            break

        batch_failed = 0

        # Phase C (short session): re-load rows and apply writes, re-checking
        # preconditions because state may have changed between phases.
        async with get_session() as session:
            for (user_id, clerk_user_id), fetch_result in zip(
                candidates, fetched, strict=True
            ):
                if isinstance(fetch_result, BaseException) and not isinstance(
                    fetch_result, Exception
                ):
                    raise fetch_result
                exc = fetch_result if isinstance(fetch_result, Exception) else None
                identity = None if exc is not None else fetch_result

                if identity is None:
                    # Non-definitive Clerk answer (error / transient): retry next
                    # run, stamp nothing.
                    summary.failed += 1
                    batch_failed += 1
                    if failure_warnings_logged < MAX_FAILURE_WARNINGS:
                        failure_warnings_logged += 1
                        cause = (
                            repr(exc)
                            if exc is not None
                            else "no identity (transient / non-github); see prior "
                            "'Failed to fetch Clerk user' warning if logged"
                        )
                        logger.warning(
                            "github_id backfill: Clerk fetch failed for user %s "
                            "(clerk_user_id=%s): %s",
                            user_id,
                            clerk_user_id,
                            cause,
                        )
                    continue

                user = await session.get(UserModel, user_id)
                # Re-check ALL Phase A preconditions: the row may have been
                # deleted / deactivated, had its github_id filled, been re-linked
                # to a different Clerk user (email-based provisioning can rewrite
                # clerk_user_id, making our fetched identity someone else's), or
                # been freshly stamped checked-absent by a login refresh /
                # webhook (a newer definitive no-github signal that must not be
                # overwritten with the older Phase B answer).
                raw_cache = (
                    user.attribution_cache
                    if user is not None and isinstance(user.attribution_cache, dict)
                    else {}
                )
                if (
                    user is None
                    or not user.is_active
                    or user.deleted_at is not None
                    or user.github_id
                    or user.clerk_user_id != clerk_user_id
                    or _marker_is_fresh(raw_cache.get("github_id_checked"))
                ):
                    summary.skipped += 1
                    continue

                if not identity.github_id:
                    if not identity.username:
                        # Definitive no-github: stamp so the scan stops
                        # re-selecting.
                        _mark_github_id_checked(user)
                    # Username present but id absent is a partial answer: leave it
                    # unstamped so a later run retries once Clerk reports the id.
                    summary.skipped += 1
                    continue

                before = user.github_id
                # Collision-safe within org, including deleted rows.
                await _set_github_id_if_absent(session, user, identity.github_id)
                if user.github_id != before:
                    summary.set += 1
                else:
                    summary.skipped += 1

        summary.scanned += len(candidates)

        # Persistent-failure backoff (no cross-run state): if an entire batch of
        # non-trivial size failed, Clerk is likely down — stop rather than keep
        # rescanning the same window and flooding logs.
        if batch_failed == len(candidates) and len(candidates) >= ALL_FAILED_ABORT_FLOOR:
            logger.warning(
                "github_id backfill: entire batch of %s Clerk fetches failed; "
                "stopping run early (Clerk likely unreachable)",
                len(candidates),
            )
            break

        if len(candidates) < remaining:
            break

    if summary.set or summary.failed:
        logger.info(
            "github_id backfill: scanned=%s set=%s skipped=%s failed=%s",
            summary.scanned,
            summary.set,
            summary.skipped,
            summary.failed,
        )
    return summary
