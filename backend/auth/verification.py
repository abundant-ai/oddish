from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    APIKeyModel,
    APIKeyScope,
    OrganizationModel,
    UserModel,
    UserRole,
    hash_api_key,
)
from oddish.cache import TTLCache
from oddish.db import utcnow
from oddish.timing import RequestTimedAsyncClient


from auth.types import AuthMethod

# =============================================================================
# Clerk Configuration
# =============================================================================

# Clerk domain (e.g., "your-app.clerk.accounts.dev")
CLERK_DOMAIN = os.getenv("CLERK_DOMAIN", "")
CLERK_ISSUER = os.getenv("CLERK_ISSUER", "").strip()
CLERK_JWT_AUDIENCE = os.getenv("CLERK_JWT_AUDIENCE", "").strip()

# JWKS cache (simple in-memory cache)
_jwks_cache: dict | None = None
_jwks_cache_time: float = 0
JWKS_CACHE_TTL = 3600  # 1 hour

# =============================================================================
# Auth Context Cache
# =============================================================================
# Cache validated auth contexts to avoid repeated DB queries.
# Key: (clerk_user_id, clerk_org_id) for JWT or api_key_hash for API keys
#
# Two lifetimes, because the two credentials revoke differently. An API key
# has no expiry of its own, so revoking one must bite within a minute: 60 s.
# A Clerk session token is verified locally on every request and expires on
# its own within about a minute, so the cache entry only maps Clerk ids to
# internal ids; role and email are taken from the freshly verified token on
# every hit. That lets identities live for 15 minutes, which is what turns
# the per-container miss rate on dashboard traffic (measured at 80-90% with
# 60 s) into a handful of database sessions per container per hour.

AUTH_CACHE_TTL = 60  # API keys: short enough to pick up a revocation
AUTH_IDENTITY_TTL = int(os.getenv("ODDISH_AUTH_IDENTITY_TTL_SECONDS", "900"))


@dataclass
class CachedAuthData:
    """Lightweight auth data for caching (no ORM objects)."""

    method: AuthMethod
    org_id: str
    org_slug: str | None = None
    user_id: str | None = None
    user_email: str | None = None
    user_role: UserRole | None = None
    api_key_id: str | None = None
    api_key_created_by_role: str | None = None
    bound_analysis_trial_id: str | None = None
    scope: APIKeyScope = APIKeyScope.FULL


_AUTH_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth

_auth_cache: TTLCache[str, CachedAuthData] = TTLCache(
    AUTH_CACHE_TTL, max_size=_AUTH_CACHE_MAX_SIZE
)


def get_cached_auth(cache_key: str) -> CachedAuthData | None:
    """Get cached auth data if still valid."""
    return _auth_cache.get(cache_key)


def set_cached_auth(
    cache_key: str, data: CachedAuthData, *, ttl_seconds: float | None = None
) -> None:
    """Cache auth data; ``ttl_seconds`` overrides the API-key default."""
    _auth_cache.set(cache_key, data, ttl_seconds=ttl_seconds)


def invalidate_cached_clerk_auth(clerk_user_id: str) -> int:
    """Drop every cached auth context for a Clerk user (all org variants).

    Only clears this process's in-memory cache; other containers hold their
    own entries until ``AUTH_IDENTITY_TTL`` lapses -- but a deleted or
    deactivated Clerk account stops receiving new session tokens, and every
    request verifies its token first, so the real bound is the token's own
    lifetime. Used on account deletion so the deleting container stops
    honoring the user immediately.
    """
    prefix = f"clerk:{clerk_user_id}:"
    return _auth_cache.invalidate_where(lambda key: key.startswith(prefix))


# =============================================================================
# Clerk JWT Verification
# =============================================================================


async def get_clerk_jwks() -> dict:
    """Fetch and cache Clerk JWKS (JSON Web Key Set)."""
    global _jwks_cache, _jwks_cache_time

    now = time.time()
    if _jwks_cache and (now - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    if not CLERK_DOMAIN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CLERK_DOMAIN not configured",
        )

    jwks_url = f"https://{CLERK_DOMAIN}/.well-known/jwks.json"

    async with RequestTimedAsyncClient() as client:
        response = await client.get(jwks_url)
        response.raise_for_status()
        _jwks_cache = response.json()
        _jwks_cache_time = now
        return _jwks_cache


async def verify_clerk_jwt(token: str) -> dict:
    """
    Verify a Clerk JWT and return the claims.

    Returns a dict with:
    - sub: Clerk user ID
    - org_id: Clerk organization ID (if user is in an org)
    - org_role: Clerk org role (if present)
    - email: User's email (if present)
    """
    try:
        jwks = await get_clerk_jwks()

        # Get the key ID from the token header
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid JWT: missing key ID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Find the matching key
        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

        if not rsa_key:
            # Key not found, try refreshing JWKS
            global _jwks_cache
            _jwks_cache = None
            jwks = await get_clerk_jwks()
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    rsa_key = key
                    break

        if not rsa_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid JWT: key not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Pin issuer to our Clerk tenant and only enforce audience when configured.
        base_issuer = CLERK_ISSUER or f"https://{CLERK_DOMAIN}".rstrip("/")
        allowed_issuers = [base_issuer, f"{base_issuer}/"]
        claims = None
        last_jwt_error: JWTError | None = None

        for issuer in allowed_issuers:
            try:
                claims = jwt.decode(
                    token,
                    rsa_key,
                    algorithms=["RS256"],
                    audience=CLERK_JWT_AUDIENCE or None,
                    issuer=issuer,
                    options={
                        "verify_aud": bool(CLERK_JWT_AUDIENCE),
                        "verify_iss": True,
                    },
                )
                break
            except JWTError as jwt_error:
                last_jwt_error = jwt_error

        if claims is None:
            if last_jwt_error:
                raise last_jwt_error
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid JWT",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return claims

    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid JWT: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to verify JWT: {str(e)}",
        )


# =============================================================================
# API Key Verification
# =============================================================================


async def verify_api_key(
    session: AsyncSession,
    raw_key: str,
) -> tuple[APIKeyModel, OrganizationModel, UserModel | None] | None:
    """
    Verify an API key and return the key + org if valid.

    Returns None if:
    - Key not found
    - Key is inactive
    - Key is expired
    - Org is inactive
    """
    key_hash = hash_api_key(raw_key)

    # Fetch API key with org
    result = await session.execute(
        select(APIKeyModel)
        .where(APIKeyModel.key_hash == key_hash)
        .where(APIKeyModel.is_active == True)  # noqa: E712
    )
    api_key = result.scalar_one_or_none()

    if api_key is None:
        return None

    # Check expiry
    if api_key.expires_at and api_key.expires_at < utcnow():
        return None

    # Load org
    org_result = await session.execute(
        select(OrganizationModel)
        .where(OrganizationModel.id == api_key.org_id)
        .where(OrganizationModel.is_active == True)  # noqa: E712
    )
    org = org_result.scalar_one_or_none()

    if org is None:
        return None

    creator = None
    if api_key.created_by_user_id:
        creator_result = await session.execute(
            select(UserModel).where(UserModel.id == api_key.created_by_user_id)
        )
        creator = creator_result.scalar_one_or_none()

    # Update last_used_at
    api_key.last_used_at = utcnow()

    return api_key, org, creator
