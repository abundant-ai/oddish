from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import APIKeyModel, APIKeyScope, utcnow


def generate_api_key() -> str:
    app_name = os.environ.get("MODAL_APP_NAME", "")
    env_marker = ""
    if app_name.startswith("oddish-pr-"):
        env_marker = f"{app_name[len('oddish-'):]}_"
    return f"ok_{env_marker}{secrets.token_hex(16)}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(
    org_id: str,
    name: str,
    scope: APIKeyScope = APIKeyScope.FULL,
    created_by_user_id: str | None = None,
    expires_at: datetime | None = None,
    is_internal: bool = False,
) -> tuple[APIKeyModel, str]:
    raw_key = generate_api_key()
    api_key = APIKeyModel(
        org_id=org_id,
        name=name,
        key_prefix=raw_key[:11],
        key_hash=hash_api_key(raw_key),
        scope=scope,
        created_by_user_id=created_by_user_id,
        expires_at=expires_at,
        is_internal=is_internal,
    )
    return api_key, raw_key


async def mint_internal_read_key(
    session: AsyncSession, *, org_id: str, name: str, ttl_minutes: int
) -> tuple[str, str]:
    """Mint + persist a READ-scoped internal key. Returns (api_key_id, raw_key)."""
    api_key, raw_key = create_api_key(
        org_id=org_id,
        name=name,
        scope=APIKeyScope.READ,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes),
        is_internal=True,
    )
    session.add(api_key)
    await session.commit()
    return api_key.id, raw_key
