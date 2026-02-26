from __future__ import annotations

from pydantic import BaseModel


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
    role: str
    org_id: str
    created_at: str


class InviteUserRequest(BaseModel):
    """Request to invite a user to the organization."""

    email: str
    name: str | None = None
    role: str = "member"  # owner, admin, or member


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


class ExperimentUpdateRequest(BaseModel):
    """Request to update experiment metadata."""

    name: str


class ExperimentUpdateResponse(BaseModel):
    """Experiment update response."""

    id: str
    name: str


class PublicExperimentResponse(BaseModel):
    """Public experiment metadata."""

    name: str
    public_token: str


class PublicExperimentListItem(BaseModel):
    """Public dataset list item."""

    id: str
    name: str
    public_token: str
    task_count: int
    created_at: str
