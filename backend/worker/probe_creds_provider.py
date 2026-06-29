"""Cloud implementation of probe API-key minting, injected into the core worker.

``api_keys`` is a hosted-only table that the core worker never imports. The
core probe-creds path (``oddish.worker.probe_creds``) exposes a provider slot;
the hosted worker fills it here at startup so probe agents can mint short-lived
read keys. See ``backend/worker/functions.py`` for the registration call.
"""

from __future__ import annotations

from oddish.db import get_session
from oddish.worker.probe_creds import register_probe_creds_provider

from models import APIKeyModel, mint_internal_read_key


async def _mint_read_key(*, org_id: str, name: str, ttl_minutes: int) -> tuple[str, str]:
    async with get_session() as session:
        return await mint_internal_read_key(
            session, org_id=org_id, name=name, ttl_minutes=ttl_minutes
        )


async def _delete_key(api_key_id: str) -> None:
    async with get_session() as session:
        key = await session.get(APIKeyModel, api_key_id)
        if key is not None:
            await session.delete(key)
            await session.commit()


def register() -> None:
    register_probe_creds_provider(mint_read_key=_mint_read_key, delete_key=_delete_key)
