from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from models import APIKeyScope, UserRole, hash_api_key
from oddish.db import get_session

from auth.provisioning import get_or_create_user_from_clerk
from auth.types import AuthContext, AuthMethod
from auth.verification import (
    CachedAuthData,
    get_cached_auth,
    set_cached_auth,
    verify_api_key,
    verify_clerk_jwt,
)


async def get_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    x_clerk_authorization: Annotated[str | None, Header()] = None,
    x_authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """
    Extract and validate authentication from request.

    Supports:
    - Bearer token (API key): Authorization: Bearer ok_<key>
    - No auth (for health checks): returns anonymous context

    Uses in-memory caching to avoid repeated DB queries for the same user/key.

    Raises HTTPException for invalid credentials.
    """
    auth_header = authorization or x_clerk_authorization or x_authorization

    # No auth header - anonymous
    if auth_header is None:
        return AuthContext(method=AuthMethod.ANONYMOUS)

    # Parse Bearer token
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:]  # Remove "Bearer " prefix

    # API Key authentication (starts with "ok_")
    if token.startswith("ok_"):
        # Cache key based on key hash (stable across requests)
        cache_key = f"apikey:{hash_api_key(token)}"

        # Check cache first
        cached = get_cached_auth(cache_key)
        if cached:
            return AuthContext(
                method=cached.method,
                org_id=cached.org_id,
                api_key_id=cached.api_key_id,
                scope=cached.scope,
                # Note: org/api_key ORM objects not included in cached response
                # Endpoints should use org_id/api_key_id for queries
            )

        # Cache miss - validate and cache
        async with get_session() as session:
            result = await verify_api_key(session, token)

            if result is None:
                # Only show "ok_***" like standard SaaS apps (Stripe, etc.)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            api_key, org = result
            await session.commit()  # Persist last_used_at update

            # Cache the result
            set_cached_auth(
                cache_key,
                CachedAuthData(
                    method=AuthMethod.API_KEY,
                    org_id=org.id,
                    api_key_id=api_key.id,
                    scope=api_key.scope,
                ),
            )

            return AuthContext(
                method=AuthMethod.API_KEY,
                org_id=org.id,
                org=org,
                api_key_id=api_key.id,
                api_key=api_key,
                scope=api_key.scope,
            )

    # Clerk JWT authentication (JWT format - contains dots)
    if "." in token:
        # Verify the JWT first (uses cached JWKS, fast)
        claims = await verify_clerk_jwt(token)

        clerk_user_id = claims.get("sub")
        clerk_org_id = claims.get("org_id")
        email = claims.get("email")
        org_role = claims.get("org_role")

        if not clerk_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid JWT: missing user ID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Cache key based on Clerk IDs (stable across token refreshes)
        cache_key = f"clerk:{clerk_user_id}:{clerk_org_id or 'no-org'}"

        # Check cache first (after JWT validation to ensure token is valid)
        cached = get_cached_auth(cache_key)
        if cached:
            return AuthContext(
                method=cached.method,
                org_id=cached.org_id,
                user_id=cached.user_id,
                user_role=cached.user_role,
                scope=cached.scope,
                # Note: org/user ORM objects not included in cached response
            )

        # Cache miss - lookup user/org and cache
        async with get_session() as session:
            result = await get_or_create_user_from_clerk(
                session, clerk_user_id, clerk_org_id, email, org_role
            )

            if result is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Organization not found. Create or select an organization "
                        "in Clerk, then ensure it has been provisioned in Oddish."
                    ),
                )

            user, org = result
            await session.commit()

            # Cache the result
            set_cached_auth(
                cache_key,
                CachedAuthData(
                    method=AuthMethod.CLERK_JWT,
                    org_id=org.id,
                    user_id=user.id,
                    user_role=user.role,
                    scope=APIKeyScope.FULL,
                ),
            )

            return AuthContext(
                method=AuthMethod.CLERK_JWT,
                org_id=org.id,
                org=org,
                user_id=user.id,
                user=user,
                user_role=user.role,
                scope=APIKeyScope.FULL,
            )

    # Unknown token format
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unsupported token format",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_auth(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    """
    Require authentication for an endpoint.

    Use as a dependency:
        @app.get("/tasks")
        async def list_tasks(auth: AuthContext = Depends(require_auth)):
            ...
    """
    if not auth.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


async def require_admin(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AuthContext:
    """
    Require admin or owner role for an endpoint.

    API keys are authorized via scope instead of user roles.
    """
    if auth.method == AuthMethod.API_KEY:
        auth.require_scope(APIKeyScope.FULL)
        return auth

    role = auth.user.role if auth.user else auth.user_role
    if role in {UserRole.ADMIN, UserRole.OWNER}:
        return auth

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin or owner role required",
    )


async def require_owner(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AuthContext:
    """
    Require the owner (developer/superuser) role for an endpoint.

    API key auth is rejected — only Clerk JWT users with the owner role
    can access owner-gated endpoints.
    """
    if auth.method == AuthMethod.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role required — API key auth is not sufficient",
        )

    role = auth.user.role if auth.user else auth.user_role
    if role == UserRole.OWNER:
        return auth

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Owner role required",
    )


__all__ = [
    "APIKeyScope",
    "AuthContext",
    "AuthMethod",
    "require_admin",
    "require_auth",
    "require_owner",
    "get_auth_context",
]
