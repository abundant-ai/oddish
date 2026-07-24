"""Backend BYOK resolver, registered into the core seam at worker startup.

Decides, per trial: for each candidate owner -- the task submitter, then the
experiment owner -- whose ``oddish_byok`` gate is on and who has a stored
Anthropic key, inject that key. Otherwise return None and the trial runs on
the platform key. Fully fail-open -- a missing key, a decrypt failure, or a
Bedrock-routed trial all just fall through to the next candidate or the
platform key, never a failure.
"""

from __future__ import annotations

import logging

import crypto
import statsig_client
from oddish.config import settings
from oddish.workers.queue.byok import (
    ByokResolution,
    register_byok_resolver,
    uses_direct_anthropic,
)

logger = logging.getLogger(__name__)


def _gate_passes(**ctx) -> bool:  # seam for tests
    return statsig_client.byok_gate_passes(**ctx)


async def _fetch_key_row(user_id: str):
    """The user's live (non-deleted) Anthropic key row, or None. The
    soft-delete filter is applied at the session level."""
    from sqlalchemy import select

    from models import UserProviderKeyModel
    from oddish.db import get_session

    async with get_session() as session:
        result = await session.execute(
            select(UserProviderKeyModel)
            .where(UserProviderKeyModel.user_id == user_id)
            .where(UserProviderKeyModel.vendor == "anthropic")
        )
        return result.scalar_one_or_none()


async def resolve_byok_for_trial(
    *,
    owner_user_id: str | None,
    org_id: str | None,
    experiment_name: str | None,
    model: str | None,
    agent: str,
    experiment_owner_user_id: str | None = None,
) -> ByokResolution | None:
    if not uses_direct_anthropic(agent, model, settings=settings):
        return None  # An Anthropic key only helps a direct-Anthropic trial.

    # Candidate owners in priority order: the task submitter, then the
    # experiment owner. A trial appended into someone's experiment can use that
    # owner's stored key when the task itself belongs to a user without one.
    # Each candidate must pass the gate for themselves; any miss (gate off, no
    # key row, decrypt failure) falls through to the next candidate, and an
    # empty candidate list keeps the platform key -- BYOK stays per-user and
    # ownerless trials never gain a key.
    candidates: list[str] = []
    for uid in (owner_user_id, experiment_owner_user_id):
        if uid and uid not in candidates:
            candidates.append(uid)

    for uid in candidates:
        if not _gate_passes(
            user_id=uid,
            org_id=org_id,
            experiment_name=experiment_name,
            model=model,
            agent=agent,
        ):
            continue
        row = await _fetch_key_row(uid)
        if row is None:
            continue
        try:
            key = crypto.decrypt_secret(row.ciphertext, row.key_version)
        except Exception:
            logger.warning(
                "could not decrypt BYOK key for user %s; trying next candidate",
                uid,
                exc_info=True,
            )
            continue
        return ByokResolution(env={"ANTHROPIC_API_KEY": key})

    return None


def install_byok_resolver() -> None:
    register_byok_resolver(resolve_byok_for_trial)
