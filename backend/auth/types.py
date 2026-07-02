from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, status

from models import APIKeyModel, APIKeyScope, OrganizationModel, UserModel, UserRole


class AuthMethod(str, Enum):
    """How the request was authenticated."""

    API_KEY = "api_key"
    CLERK_JWT = "clerk_jwt"
    ANONYMOUS = "anonymous"  # For public endpoints


@dataclass
class AuthContext:
    """Authentication context for a request."""

    method: AuthMethod
    org_id: str | None = None
    org: OrganizationModel | None = None
    org_slug: str | None = None
    user_id: str | None = None
    user: UserModel | None = None
    user_email: str | None = None
    user_role: UserRole | None = None
    api_key_id: str | None = None
    api_key: APIKeyModel | None = None
    scope: APIKeyScope = APIKeyScope.FULL

    @property
    def is_authenticated(self) -> bool:
        return self.org_id is not None

    @property
    def is_member_created_api_key(self) -> bool:
        """Return whether this request uses a TASKS key minted by a non-admin."""
        return (
            self.method == AuthMethod.API_KEY
            and self.scope == APIKeyScope.TASKS
            and self.user_id is not None
            and self.user_role != UserRole.ADMIN
        )

    def require_scope(
        self,
        required: APIKeyScope,
        *,
        allow_member_created_task_key: bool = True,
    ) -> None:
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
        if (
            required == APIKeyScope.TASKS
            and not allow_member_created_task_key
            and self.is_member_created_api_key
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Member-created API keys cannot perform this operation",
            )
