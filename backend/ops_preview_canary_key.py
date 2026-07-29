"""Mint or revoke a short-lived API key for the PR-preview hosted canary.

This is temporary validation plumbing for PR #966. The raw key is generated
and masked by GitHub Actions before it reaches this function; only its hash is
persisted. The preview database secret is attached by ``modal_app``.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import modal
from modal_app import image, runtime_secrets

app = modal.App("preview-harbor-canary-key")

_CANARY_ORG_ID = "8ebde5d0"
_CANARY_USER_ID = "user_pr966_harbor_canary"
_CANARY_USER_EMAIL = "pr-966-harbor-canary@oddish.invalid"


@app.function(image=image, secrets=runtime_secrets, timeout=120)
async def mutate_key(action: str, raw_key: str) -> str:
    if action not in {"mint", "revoke"}:
        raise ValueError(f"unsupported action: {action!r}")
    if not raw_key.startswith("ok_pr-966_canary_"):
        raise ValueError("refusing a key outside the PR #966 canary namespace")

    from oddish.config import Settings

    Settings.db_use_null_pool = True

    from models import OrganizationModel, UserModel, UserRole
    from oddish.db import get_session, utcnow
    from oddish.db.models import APIKeyModel, APIKeyScope
    from sqlalchemy import select

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with get_session() as session:
        existing = (
            await session.execute(
                select(APIKeyModel)
                .where(APIKeyModel.key_hash == key_hash)
                .execution_options(include_deleted=True)
            )
        ).scalar_one_or_none()

        if action == "mint":
            org_exists = (
                await session.execute(
                    select(OrganizationModel.id).where(
                        OrganizationModel.id == _CANARY_ORG_ID
                    )
                )
            ).scalar_one_or_none()
            if org_exists is None:
                raise RuntimeError(
                    f"preview canary organization {_CANARY_ORG_ID!r} is missing"
                )
            if existing is not None:
                raise RuntimeError("preview canary key already exists")

            canary_user = await session.get(UserModel, _CANARY_USER_ID)
            if canary_user is None:
                canary_user = UserModel(
                    id=_CANARY_USER_ID,
                    org_id=_CANARY_ORG_ID,
                    email=_CANARY_USER_EMAIL,
                    name="PR #966 Harbor canary",
                    role=UserRole.MEMBER,
                    is_active=True,
                )
                session.add(canary_user)
                await session.flush()
            elif (
                canary_user.org_id != _CANARY_ORG_ID
                or canary_user.email != _CANARY_USER_EMAIL
                or not canary_user.is_active
                or canary_user.deleted_at is not None
            ):
                raise RuntimeError("preview canary user exists in an invalid state")

            api_key = APIKeyModel(
                org_id=_CANARY_ORG_ID,
                name="PR #966 hosted Harbor canary",
                key_prefix=raw_key[:16],
                key_hash=key_hash,
                scope=APIKeyScope.FULL,
                created_by_user_id=_CANARY_USER_ID,
                created_by_role=UserRole.MEMBER.value,
                expires_at=utcnow() + timedelta(minutes=75),
                is_internal=True,
            )
            session.add(api_key)
            await session.commit()
            return api_key.id

        if existing is None:
            raise RuntimeError("preview canary key is missing during revocation")
        if not existing.is_active or existing.deleted_at is not None:
            raise RuntimeError("preview canary key was already revoked")
        existing.is_active = False
        existing.deleted_at = utcnow()
        await session.commit()
        return existing.id


@app.local_entrypoint()
def main(action: str, raw_key: str) -> None:
    key_id = mutate_key.remote(action=action, raw_key=raw_key)
    if not key_id:
        raise RuntimeError(f"{action} returned an empty API key id")
    print(f"{action} succeeded for API key id {key_id}")
