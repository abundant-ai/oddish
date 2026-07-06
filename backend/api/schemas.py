from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


# =============================================================================
# Organization Models
# =============================================================================


class OrganizationResponse(BaseModel):
    """Organization response."""

    id: str
    name: str
    slug: str
    plan: str
    created_at: str


# =============================================================================
# User Models
# =============================================================================


class UserResponse(BaseModel):
    """User response."""

    id: str
    email: str
    name: str | None
    github_username: str | None
    github_id: str | None = None
    role: str
    org_id: str
    created_at: str


class QuotaUsageResponse(BaseModel):
    user_id: str
    # Effective limit: base (default or override) + live temporary bumps.
    limit_usd: float
    used_usd: float
    # In-flight trial reservations; admission blocks when used + reserved >= limit.
    reserved_usd: float = 0
    # Whether exceeding the limit actually blocks new billable runs (quota_mode ==
    # enforce). False under off/shadow, so the UI must not claim runs are blocked.
    enforced: bool = False
    # Base limit (override row or org default), before any bumps.
    base_limit_usd: float = 0
    # SUM of the caller's live temporary bump amounts (0 when none).
    bump_usd: float = 0
    # ISO datetime of the latest live bump expiry, or null when no live bump.
    bump_expires_at: str | None = None


class QuotaMemberItem(BaseModel):
    user_id: str
    email: str
    name: str | None
    github_username: str | None
    role: str
    # Effective limit: base + live bumps.
    limit_usd: float
    used_usd: float
    # Base limit (override row or org default), before any bumps.
    base_limit_usd: float = 0
    # SUM of live bump amounts (0 when none).
    bump_usd: float = 0
    # ISO datetime of the latest live bump expiry, or null when no live bump.
    bump_expires_at: str | None = None


class QuotaListResponse(BaseModel):
    members: list[QuotaMemberItem]


class QuotaUpdateRequest(BaseModel):
    limit_usd: Decimal | None = Field(
        None, gt=0, le=Decimal("99999999.9999"), max_digits=12, decimal_places=4
    )


class QuotaBumpRequest(BaseModel):
    """Grant a temporary additive boost to a member's 24h limit."""

    amount_usd: Decimal = Field(
        gt=0, le=Decimal("99999999.9999"), max_digits=12, decimal_places=4
    )
    expires_at: datetime
    reason: str | None = Field(None, max_length=500)


class InviteUserRequest(BaseModel):
    """Request to invite a user to the organization."""

    email: str
    name: str | None = None
    role: str = "member"  # admin or member


class InviteUserResponse(BaseModel):
    """Response for a Clerk organization invitation."""

    invitation_id: str
    email: str
    role: str
    status: str


# =============================================================================
# API Key Models
# =============================================================================


class APIKeyResponse(BaseModel):
    """API key response (without the key itself)."""

    id: str
    name: str
    key_prefix: str
    scope: str
    org_id: str
    is_active: bool
    expires_at: str | None
    last_used_at: str | None
    created_at: str


class APIKeyCreateResponse(BaseModel):
    """API key creation response (includes the key - shown once!)."""

    id: str
    name: str
    key: str  # Only shown on creation!
    key_prefix: str
    scope: str
    org_id: str
    expires_at: str | None
    created_at: str


class APIKeyPermissionsResponse(BaseModel):
    """API key capability flags for the current user."""

    can_create: bool
    can_manage: bool
    allowed_scopes: list[str]


class CreateAPIKeyRequest(BaseModel):
    """Request to create an API key."""

    name: str
    scope: str = "full"  # full, tasks, or read
    expires_in_days: int | None = None


# =============================================================================
# Experiment Sharing Models
# =============================================================================


class ExperimentShareResponse(BaseModel):
    """Experiment share status for the org."""

    name: str
    is_public: bool
    public_token: str | None = None
    description: str | None = None


class ExperimentUpdateRequest(BaseModel):
    """Request to update experiment metadata.

    Both fields are optional so callers can patch ``name`` and
    ``description`` independently without clobbering the other.
    """

    name: str | None = None
    description: str | None = None


class ExperimentUpdateResponse(BaseModel):
    """Experiment update response."""

    id: str
    name: str
    description: str | None = None
