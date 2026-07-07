"""Backend BYOK resolver, registered into the core seam at worker startup.

Decides, per trial: if the ``oddish_byok`` gate is on for the trial's owner and
they have a stored Anthropic key, inject it. Otherwise return None and the
trial runs on the platform key. Fully fail-open -- a missing key, a decrypt
failure, or a Bedrock-routed trial all just fall back to the platform key,
never a failure.
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
) -> ByokResolution | None:
    if not owner_user_id:
        return None  # BYOK is per-user; ownerless trials use platform keys.
    if not uses_direct_anthropic(agent, model, settings=settings):
        return None  # An Anthropic key only helps a direct-Anthropic trial.
    if not _gate_passes(
        user_id=owner_user_id,
        org_id=org_id,
        experiment_name=experiment_name,
        model=model,
        agent=agent,
    ):
        return None

    row = await _fetch_key_row(owner_user_id)
    if row is None:
        return None
    try:
        key = crypto.decrypt_secret(row.ciphertext, row.key_version)
    except Exception:
        logger.warning(
            "could not decrypt BYOK key for user %s; using platform key",
            owner_user_id,
            exc_info=True,
        )
        return None

    return ByokResolution(env={"ANTHROPIC_API_KEY": key})


def install_byok_resolver() -> None:
    register_byok_resolver(resolve_byok_for_trial)
