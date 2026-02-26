from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, status

from models import APIKeyModel, APIKeyScope, OrganizationModel, UserModel, UserRole


class AuthMethod(str, Enum):
    """How the request was authenticated."""

    API_KEY = "api_key"
    CLERK_JWT = "clerk_jwt"
    ANONYMOUS = "anonymous"  # For health checks, public endpoints


@dataclass
class AuthContext:
    """Authentication context for a request."""

    method: AuthMethod
    org_id: str | None = None
    org: OrganizationModel | None = None
    user_id: str | None = None
    user: UserModel | None = None
    user_role: UserRole | None = None
    api_key_id: str | None = None
    api_key: APIKeyModel | None = None
    scope: APIKeyScope = APIKeyScope.FULL

    @property
    def is_authenticated(self) -> bool:
        return self.org_id is not None

    def require_scope(self, required: APIKeyScope) -> None:
        """Raise 403 if current scope is insufficient."""
        # Scope hierarchy: FULL > TASKS > READ
        scope_level = {APIKeyScope.READ: 1, APIKeyScope.TASKS: 2, APIKeyScope.FULL: 3}
        if scope_level.get(self.scope, 0) < scope_level.get(required, 3):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Insufficient scope. "
                    f"Required: {required.value}, got: {self.scope.value}"
                ),
            )
