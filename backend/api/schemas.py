from __future__ import annotations

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
    limit_usd: float
    used_usd: float
    # In-flight trial reservations; admission blocks when used + reserved >= limit.
    reserved_usd: float = 0
    # Whether exceeding the limit actually blocks new billable runs (quota_mode ==
    # enforce). False under off/shadow, so the UI must not claim runs are blocked.
    enforced: bool = False


class QuotaMemberItem(BaseModel):
    user_id: str
    email: str
    name: str | None
    github_username: str | None
    role: str
    limit_usd: float
    used_usd: float


class QuotaListResponse(BaseModel):
    members: list[QuotaMemberItem]
    # Org-wide aggregate MONTHLY cap. ``org_limit_usd`` is the effective cap
    # (override row or configured default); ``None`` means no org cap.
    # ``org_used_usd`` is month-to-date settled org-wide spend.
    org_limit_usd: float | None = None
    org_used_usd: float = 0
    org_reserved_usd: float = 0
    org_default_limit_usd: float | None = None


class OrgQuotaResponse(BaseModel):
    """Admin-facing org cap fields returned by ``PUT /quotas/org``."""

    org_limit_usd: float | None = None
    org_used_usd: float = 0
    org_reserved_usd: float = 0
    org_default_limit_usd: float | None = None


class OrgUsageResponse(BaseModel):
    """Member-visible org budget snapshot for the dashboard goal bar
    (``GET /quotas/org``)."""

    # Effective monthly cap (override ?? default ?? null). None = no org cap.
    org_limit_usd: float | None = None
    # Settled org-wide spend since the start of the UTC month.
    org_used_month_usd: float = 0
    # Org-wide in-flight reservation (not day/month bound).
    org_reserved_usd: float = 0
    # Settled org-wide spend since UTC midnight today.
    org_used_today_usd: float = 0
    # Adaptive pace target; null when no cap. max(0, limit - spend-before-today)
    # / days_remaining.
    daily_goal_usd: float | None = None
    # Days left in the UTC month, INCLUDING today.
    days_remaining: int = 0
    # quota_mode == enforce.
    enforced: bool = False


class QuotaUpdateRequest(BaseModel):
    limit_usd: Decimal | None = Field(
        None, gt=0, le=Decimal("99999999.9999"), max_digits=12, decimal_places=4
    )


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
# BYOK Models
# =============================================================================


class ByokStatusResponse(BaseModel):
    """BYOK state for the current user -- never the key itself."""

    enabled: bool = False  # whether the oddish_byok gate is on for this user
    key_set: bool = False
    key_hint: str = ""  # last 4 chars, for display only


class PutByokKeyRequest(BaseModel):
    key: str


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
