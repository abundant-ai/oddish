from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import APIKeyModel, APIKeyScope, OrganizationModel, UserRole, hash_api_key
from oddish.db import utcnow

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
# Value: (CachedAuthData, timestamp)

AUTH_CACHE_TTL = 60  # 60 seconds - short enough to pick up permission changes


@dataclass
class CachedAuthData:
    """Lightweight auth data for caching (no ORM objects)."""

    method: AuthMethod
    org_id: str
    user_id: str | None = None
    user_role: UserRole | None = None
    api_key_id: str | None = None
    scope: APIKeyScope = APIKeyScope.FULL


_AUTH_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth


class _TTLCache:
    """Simple TTL + size-bounded cache for auth context."""

    def __init__(self, ttl_seconds: int, max_size: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[CachedAuthData, float]] = OrderedDict()

    def get(self, key: str) -> CachedAuthData | None:
        now = time.time()
        entry = self._data.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if expires_at <= now:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: CachedAuthData) -> None:
        now = time.time()
        self._data[key] = (value, now + self._ttl_seconds)
        self._data.move_to_end(key)
        self._purge(now)

    def _purge(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._data.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)


_auth_cache = _TTLCache(AUTH_CACHE_TTL, _AUTH_CACHE_MAX_SIZE)


def get_cached_auth(cache_key: str) -> CachedAuthData | None:
    """Get cached auth data if still valid."""
    return _auth_cache.get(cache_key)


def set_cached_auth(cache_key: str, data: CachedAuthData) -> None:
    """Cache auth data with current timestamp."""
    _auth_cache.set(cache_key, data)


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

    async with httpx.AsyncClient() as client:
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
) -> tuple[APIKeyModel, OrganizationModel] | None:
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

    # Update last_used_at
    api_key.last_used_at = utcnow()

    return api_key, org
