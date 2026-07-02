from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import or_, select

from auth.provisioning import (
    _mark_github_id_checked,
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


async def backfill_github_id(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_users: int | None = None,
) -> BackfillSummary:
    summary = BackfillSummary()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    after_id: str | None = None

    async def _fetch(user: UserModel):
        async with semaphore:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                return user, await fetch_github_identity_from_clerk(user.clerk_user_id), None
            except Exception as exc:  # fail-open: unexpected escaping error
                return user, None, exc

    while max_users is None or summary.scanned < max_users:
        remaining = (
            batch_size if max_users is None else min(batch_size, max_users - summary.scanned)
        )
        if remaining <= 0:
            break

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
                    )
                )
            )
            if after_id is not None:
                stmt = stmt.where(UserModel.id > after_id)
            result = await session.execute(
                stmt.order_by(UserModel.id.asc()).limit(remaining)
            )
            users = list(result.scalars().all())
            if not users:
                break
            # Advance over skipped rows so keyset pagination terminates.
            after_id = users[-1].id

            fetched = await asyncio.gather(*(_fetch(u) for u in users))
            for user, identity, exc in fetched:
                if exc is not None or identity is None:
                    # Non-definitive Clerk answer (error / unset key): retry next
                    # run, stamp nothing.
                    summary.failed += 1
                    logger.warning(
                        "github_id backfill: Clerk fetch failed for user %s: %s",
                        user.id,
                        exc,
                    )
                    continue
                if not identity.github_id:
                    # Definitive no-github: stamp so the scan stops re-selecting.
                    _mark_github_id_checked(user)
                    summary.skipped += 1
                    continue
                before = user.github_id
                # Collision-safe within org, including deleted rows.
                await _set_github_id_if_absent(session, user, identity.github_id)
                if user.github_id != before:
                    summary.set += 1
                else:
                    summary.skipped += 1

        summary.scanned += len(users)
        if len(users) < remaining:
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
