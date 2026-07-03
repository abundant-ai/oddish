"""Throttled, repeatable github_id backfill for existing handle-having users.

A plain callable (NOT migration-embedded — preview envs bootstrap via create_all +
stamp-heads and skip migration data steps; mirrors ``dashboard_owner_backfill``).
Invoked best-effort from the queue reconciler (``worker/functions.py``). Populates
``UserModel.github_id`` from Clerk for active users that have a ``clerk_user_id``
but ``github_id IS NULL``. G3 captures github_id opportunistically for new /
handle-less users on the hot path; existing handle-having users are deliberately
left for this batch pass so the latency-sensitive auth path never adds a Clerk GET.

Idempotent: the ``github_id IS NULL`` filter means a re-run only touches
still-unset rows; already-set ids are skipped; a differing id is never overwritten
(guaranteed by the shared ``_set_github_id_if_absent`` collision-safe helper).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from auth.provisioning import (
    _set_github_id_if_absent,
    fetch_github_identity_from_clerk,
)
from models import UserModel
from oddish.db import get_session

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
DEFAULT_CONCURRENCY = 4
DEFAULT_DELAY_SECONDS = 0.2


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


def _candidate_select(*, batch_size: int, after_id: str | None = None):
    # Active users with a clerk id but no github_id yet, in id order. The keyset
    # cursor (id > after_id) advances the window past rows already visited THIS
    # run — crucially the permanently-unfillable ones (Clerk has no github_id, an
    # in-org collision, a transient Clerk error) that stay github_id IS NULL. A
    # plain re-`select` of the null pool would re-fetch them forever (infinite
    # loop in drain mode, starvation of later rows under a max_users cap). A fresh
    # run restarts at after_id=None, so skipped rows are retried next cycle.
    stmt = (
        select(UserModel)
        .where(UserModel.clerk_user_id.isnot(None))
        .where(UserModel.github_id.is_(None))
        .where(UserModel.is_active == True)  # noqa: E712
    )
    if after_id is not None:
        stmt = stmt.where(UserModel.id > after_id)
    return stmt.order_by(UserModel.id.asc()).limit(batch_size)


async def backfill_github_id(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_users: int | None = None,
) -> BackfillSummary:
    """Populate github_id from Clerk for eligible users, batch by batch.

    Throttle knobs: ``concurrency`` caps simultaneous Clerk GETs; ``delay_seconds``
    spaces each admitted GET. ``max_users`` bounds a single run (None = drain).
    Commits per batch so progress survives interruption.

    No in-run retry: ``fetch_github_identity_from_clerk`` swallows transient Clerk
    errors into an empty identity, so an unreachable Clerk surfaces as a skip and
    the user is re-attempted on the reconciler's NEXT cycle (periodicity is the
    backoff). Only an unexpected escaping error counts as ``failed``.
    """
    summary = BackfillSummary()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    after_id: str | None = None

    async def _fetch(user_id: str, clerk_user_id: str):
        # Clerk GETs run concurrently (semaphore-capped) while NO DB session is
        # held (see the three-phase loop below); returns the identity or the
        # escaping error. Keyed by user_id so the write phase reloads the row.
        async with semaphore:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                return user_id, await fetch_github_identity_from_clerk(clerk_user_id), None
            except Exception as exc:  # fail-open: unexpected escaping error
                return user_id, None, exc

    while max_users is None or summary.scanned < max_users:
        remaining = (
            batch_size if max_users is None else min(batch_size, max_users - summary.scanned)
        )
        if remaining <= 0:
            break

        # Phase 1 (short read session): grab a page of candidate (id, clerk_id)
        # pairs, then RELEASE the pooled connection before any HTTP. Holding an
        # API DB connection across the Clerk fan-out would pin a scarce pooler
        # slot for the whole batch's I/O — the anti-pattern github_linkage's
        # miss path avoids and the worker-runtime invariant forbids.
        async with get_session() as session:
            users = list(
                (
                    await session.execute(
                        _candidate_select(batch_size=remaining, after_id=after_id)
                    )
                )
                .scalars()
                .all()
            )
            if not users:
                break
            targets = [(u.id, u.clerk_user_id) for u in users]
            # Advance the cursor past this page up front, so a page of all-skips
            # still moves forward (guarantees termination + reaches later rows).
            after_id = targets[-1][0]

        # Phase 2 (no session held): concurrent, throttled Clerk fetches.
        fetched = await asyncio.gather(
            *(_fetch(uid, clerk_id) for uid, clerk_id in targets)
        )

        # Phase 3 (short write session): reload each row fresh and apply writes
        # sequentially (an AsyncSession is not safe for concurrent ops). A row
        # that was set/deactivated/removed since phase 1 is a benign skip.
        async with get_session() as session:
            for user_id, identity, exc in fetched:
                if exc is not None:
                    summary.failed += 1
                    logger.warning(
                        "github_id backfill: Clerk fetch failed for user %s: %s",
                        user_id,
                        exc,
                    )
                    continue
                if identity is None or not identity.github_id:
                    summary.skipped += 1
                    continue
                user = await session.get(UserModel, user_id)
                if user is None or not user.is_active or user.github_id is not None:
                    summary.skipped += 1
                    continue
                # Collision-safe (org-unique, include_deleted): never overwrites
                # a differing id; skips a value already claimed in-org.
                await _set_github_id_if_absent(session, user, identity.github_id)
                if user.github_id is not None:
                    summary.set += 1
                else:
                    summary.skipped += 1

        summary.scanned += len(targets)
        # A short batch means the candidate pool is drained for this run.
        if len(targets) < remaining:
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
