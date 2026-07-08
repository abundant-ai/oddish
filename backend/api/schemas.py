from __future__ import annotations

from decimal import Decimal
from typing import Literal

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
    reserved_usd: float = 0
    enforced: bool = False
    base_limit_usd: float = 0
    bump_usd: float = 0
    bump_expires_at: str | None = None


class QuotaMemberItem(BaseModel):
    user_id: str
    email: str
    name: str | None
    github_username: str | None
    role: str
    limit_usd: float
    used_usd: float
    base_limit_usd: float = 0
    bump_usd: float = 0
    bump_expires_at: str | None = None


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


class QuotaBumpRequest(BaseModel):
    amount_usd: Decimal = Field(
        gt=0, le=Decimal("99999999.9999"), max_digits=12, decimal_places=4
    )
    duration_hours: int = Field(gt=0, le=8760)
    reason: str | None = Field(None, max_length=500)


class QuotaGambleRequest(BaseModel):
    # Cap keeps net_usd = wager * (max_multiplier - 1) inside NUMERIC(12, 4)
    # even at plinko's 170x (500k * 169 < 99,999,999.9999).
    wager_usd: Decimal = Field(
        gt=0, le=Decimal("500000"), max_digits=12, decimal_places=4
    )
    game: Literal["coinflip", "plinko", "slots", "baccarat", "rocket", "sling"] = (
        "coinflip"
    )
    side: Literal["heads", "tails"] = "heads"
    risk: Literal["low", "medium", "high"] = "medium"
    bet: Literal["player", "banker", "tie"] = "player"
    target: Decimal | None = Field(None, gt=1, le=Decimal("100"), decimal_places=2)
    rung: int | None = Field(None, ge=1, le=10)


class QuotaGambleResponse(BaseModel):
    game: str
    won: bool
    # Coinflip legacy fields; None for every other game.
    side: str | None = None
    result: str | None = None
    multiplier: float
    wager_usd: float
    net_usd: float
    # Post-play effective limit; used/reserved are unchanged by the play.
    limit_usd: float
    used_usd: float
    reserved_usd: float
    enforced: bool = False
    detail: dict = Field(default_factory=dict)


class QuotaGambleItem(BaseModel):
    id: str
    game: str = "coinflip"
    wager_usd: float
    net_usd: float
    multiplier: float | None = None
    won: bool
    created_at: str
    detail: dict | None = None


class QuotaGambleListResponse(BaseModel):
    items: list[QuotaGambleItem]
    # Net over the rolling 24h window (all live rows, not just the page above).
    net_usd: float


class DuelMember(BaseModel):
    user_id: str
    label: str


class DuelFight(BaseModel):
    # Deterministic replay seed; both players render the same fight.
    seed: int
    winner_role: Literal["challenger", "opponent"]


class DuelCreateRequest(BaseModel):
    opponent_user_id: str
    wager_usd: Decimal = Field(
        gt=0, le=Decimal("500000"), max_digits=12, decimal_places=4
    )


class DuelItem(BaseModel):
    id: str
    status: Literal["open", "settled", "declined", "cancelled"]
    # The CALLER's role in this duel.
    role: Literal["challenger", "opponent"]
    challenger: DuelMember
    opponent: DuelMember
    wager_usd: float
    winner_user_id: str | None = None
    i_won: bool | None = None
    my_net_usd: float | None = None
    fight: DuelFight | None = None
    created_at: str


class DuelListResponse(BaseModel):
    incoming: list[DuelItem]
    outgoing: list[DuelItem]
    recent: list[DuelItem]
    members: list[DuelMember]


class DuelAcceptResponse(BaseModel):
    duel: DuelItem
    limit_usd: float
    used_usd: float
    reserved_usd: float
    enforced: bool = False


class BlackjackRequest(BaseModel):
    action: Literal["deal", "hit", "stand", "double"]
    session_id: str | None = None
    # Required for action=deal, ignored otherwise. Same overflow cap as
    # QuotaGambleRequest (worst blackjack outcome is 2x wager at 2.5x).
    wager_usd: Decimal | None = Field(
        None, gt=0, le=Decimal("500000"), max_digits=12, decimal_places=4
    )


class BlackjackStateResponse(BaseModel):
    session_id: str
    status: Literal["player_turn", "settled"]
    player: list[str]
    # Hole card masked as "??" until the hand settles.
    dealer: list[str]
    player_total: int
    dealer_total: int | None = None
    can_double: bool = False
    outcome: Literal["win", "lose", "push", "blackjack"] | None = None
    # Total riding wager (doubles after a double).
    wager_usd: float
    net_usd: float | None = None
    multiplier: float | None = None
    limit_usd: float
    used_usd: float
    reserved_usd: float
    enforced: bool = False


class WrestleRequest(BaseModel):
    wager_usd: Decimal = Field(
        gt=0, le=Decimal("500000"), max_digits=12, decimal_places=4
    )
    # Client-reported result of a LOCAL hot-seat match (the server can't referee
    # a same-keyboard game). Deliberately trust-the-client per the feature owner.
    won: bool


class WrestleResponse(BaseModel):
    won: bool
    wager_usd: float
    net_usd: float
    limit_usd: float
    used_usd: float
    reserved_usd: float
    enforced: bool = False


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
