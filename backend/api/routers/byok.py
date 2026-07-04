"""Per-user BYOK settings API: view, save, and clear the user's Anthropic key.

User-session auth only: an oddish API key must not read or write BYOK state.
Whether the key is actually used at run time is decided by the ``oddish_byok``
Statsig gate, not here -- this endpoint only stores the key.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import crypto
import statsig_client
from api.schemas import ByokStatusResponse, PutByokKeyRequest
from auth import AuthContext, AuthMethod, require_auth
from models import UserProviderKeyModel
from oddish.db import get_session, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/byok", tags=["BYOK"])

VENDOR = "anthropic"


def _require_user_session(auth: AuthContext) -> None:
    if auth.method == AuthMethod.API_KEY:
        raise HTTPException(status_code=403, detail="BYOK requires user login")
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="BYOK requires a user identity")


async def _live_key_row(session, user_id: str):
    result = await session.execute(
        select(UserProviderKeyModel)
        .where(UserProviderKeyModel.user_id == user_id)
        .where(UserProviderKeyModel.vendor == VENDOR)
    )
    return result.scalar_one_or_none()


@router.get("", response_model=ByokStatusResponse)
async def get_byok_status(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ByokStatusResponse:
    _require_user_session(auth)
    enabled = False
    try:
        enabled = statsig_client.byok_gate_passes(auth.user_id, org_id=auth.org_id)
    except Exception:
        logger.warning("byok gate check failed; showing disabled", exc_info=True)
    async with get_session() as session:
        row = await _live_key_row(session, auth.user_id)
    return ByokStatusResponse(
        enabled=enabled,
        key_set=row is not None,
        key_hint=row.key_hint if row is not None else "",
    )


@router.put("/keys/anthropic", response_model=ByokStatusResponse)
async def put_byok_key(
    body: PutByokKeyRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ByokStatusResponse:
    _require_user_session(auth)
    key = body.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="key must not be empty")

    try:
        ciphertext, key_version = crypto.encrypt_secret(key)
    except crypto.CredentialKeyMissingError as exc:
        # The environment isn't set up for BYOK (no encryption key). Surface a
        # clear "unavailable" instead of a raw 500.
        raise HTTPException(
            status_code=503,
            detail="BYOK storage is not configured on this environment",
        ) from exc
    now = utcnow()
    try:
        async with get_session() as session:
            existing = await _live_key_row(session, auth.user_id)
            if existing is not None:
                # Retire the old row before inserting the new one: the live-row
                # index is a partial UNIQUE that PG checks per statement, so the
                # flush must land the soft-delete before the insert.
                existing.deleted_at = now
                await session.flush()
            session.add(
                UserProviderKeyModel(
                    user_id=auth.user_id,
                    org_id=auth.org_id,
                    vendor=VENDOR,
                    ciphertext=ciphertext,
                    key_version=key_version,
                    key_hint=key[-4:],
                )
            )
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="another update for this key landed at the same time; retry",
        ) from exc

    enabled = False
    try:
        enabled = statsig_client.byok_gate_passes(auth.user_id, org_id=auth.org_id)
    except Exception:
        logger.warning("byok gate check failed; showing disabled", exc_info=True)
    return ByokStatusResponse(enabled=enabled, key_set=True, key_hint=key[-4:])


@router.delete("/keys/anthropic")
async def delete_byok_key(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    _require_user_session(auth)
    async with get_session() as session:
        row = await _live_key_row(session, auth.user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no key to remove")
        row.deleted_at = utcnow()
        await session.commit()
    return {"deleted": VENDOR}
