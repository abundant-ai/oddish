from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from api.schemas import APIKeyCreateResponse, APIKeyResponse, CreateAPIKeyRequest
from auth import APIKeyScope, AuthContext, require_admin, require_owner
from models import APIKeyModel, create_api_key
from oddish.db import get_session, utcnow

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[APIKeyResponse]:
    """List all API keys for the organization."""

    async with get_session() as session:
        result = await session.execute(
            select(APIKeyModel)
            .where(APIKeyModel.org_id == auth.org_id)
            .order_by(APIKeyModel.created_at.desc())
        )
        keys = result.scalars().all()

        return [
            APIKeyResponse(
                id=k.id,
                name=k.name,
                key_prefix=k.key_prefix,
                scope=k.scope.value,
                org_id=k.org_id,
                is_active=k.is_active,
                expires_at=k.expires_at.isoformat() if k.expires_at else None,
                last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
                created_at=k.created_at.isoformat(),
            )
            for k in keys
        ]


@router.post("", response_model=APIKeyCreateResponse)
async def create_api_key_endpoint(
    request: CreateAPIKeyRequest,
    auth: Annotated[AuthContext, Depends(require_owner)],
) -> APIKeyCreateResponse:
    """Create a new API key for the organization. Requires owner (developer) role."""

    # Validate scope
    try:
        scope = APIKeyScope(request.scope)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope: {request.scope}. Must be one of: full, tasks, read",
        )

    # Calculate expiry
    expires_at = None
    if request.expires_in_days:
        expires_at = utcnow() + timedelta(days=request.expires_in_days)

    async with get_session() as session:
        api_key_model, raw_key = create_api_key(
            org_id=auth.org_id,
            name=request.name,
            scope=scope,
            created_by_user_id=auth.user_id,
            expires_at=expires_at,
        )
        session.add(api_key_model)
        await session.commit()

        return APIKeyCreateResponse(
            id=api_key_model.id,
            name=api_key_model.name,
            key=raw_key,  # Only shown once!
            key_prefix=api_key_model.key_prefix,
            scope=api_key_model.scope.value,
            org_id=auth.org_id,
            expires_at=(
                api_key_model.expires_at.isoformat()
                if api_key_model.expires_at
                else None
            ),
            created_at=api_key_model.created_at.isoformat(),
        )


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Revoke an API key."""

    async with get_session() as session:
        result = await session.execute(
            select(APIKeyModel)
            .where(APIKeyModel.id == key_id)
            .where(APIKeyModel.org_id == auth.org_id)
        )
        api_key = result.scalar_one_or_none()

        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        api_key.is_active = False
        await session.commit()

        return {"status": "revoked", "key_id": key_id}
