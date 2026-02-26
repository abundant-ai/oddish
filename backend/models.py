from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import mapped_column as mapped_column  # type: ignore[attr-defined]

# Import base from OSS oddish
from oddish.db.models import Base


def generate_id() -> str:
    """Generate a short unique ID."""
    return str(uuid4())[:8]


def generate_api_key() -> str:
    """Generate a secure API key with prefix for easy identification."""
    # Format: ok_<32 random hex chars> (64 bits of entropy)
    return f"ok_{secrets.token_hex(16)}"


# =============================================================================
# Enums
# =============================================================================


class UserRole(str, Enum):
    """User roles within an organization."""

    OWNER = "owner"  # Developer/superuser — only assignable via direct DB edit
    ADMIN = "admin"  # Can manage users and settings
    MEMBER = "member"  # Can run evals, view results


class APIKeyScope(str, Enum):
    """API key permission scopes."""

    FULL = "full"  # All operations (tasks, trials, admin)
    TASKS = "tasks"  # Create/view tasks and trials only
    READ = "read"  # Read-only access


# =============================================================================
# Cloud Models
# =============================================================================


class OrganizationModel(Base):
    """Organization (tenant) for multi-tenancy."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Clerk integration - links to Clerk organization
    clerk_org_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    # Billing/plan info (for future use)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Soft delete
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    users: Mapped[list["UserModel"]] = relationship(  # type: ignore[assignment]
        "UserModel", back_populates="organization", lazy="selectin"
    )
    api_keys: Mapped[list["APIKeyModel"]] = relationship(  # type: ignore[assignment]
        "APIKeyModel", back_populates="organization", lazy="selectin"
    )


class UserModel(Base):
    """User within an organization.

    Users are authenticated via Clerk (external), and this model
    stores the user profile and organization membership.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)

    # Clerk Auth integration
    # This is the Clerk user ID (e.g., "user_xxx")
    clerk_user_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    # Organization membership
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(
            UserRole,
            name="userrole",
            values_callable=lambda enum: [e.value for e in enum],
        ),
        default=UserRole.MEMBER,
        nullable=False,
    )

    # Profile
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    organization: Mapped["OrganizationModel"] = relationship(  # type: ignore[assignment]
        "OrganizationModel", back_populates="users", lazy="selectin"
    )
    api_keys: Mapped[list["APIKeyModel"]] = relationship(  # type: ignore[assignment]
        "APIKeyModel", back_populates="created_by_user", lazy="selectin"
    )

    __table_args__ = (
        # A user can only be in one org with one email
        UniqueConstraint("org_id", "email", name="uq_users_org_email"),
        Index("idx_users_org_id", "org_id"),
        Index("idx_users_email", "email"),
        Index("idx_users_github_username", "github_username"),
    )


class APIKeyModel(Base):
    """API key for programmatic access.

    API keys are scoped to an organization and have specific permissions.
    The actual key is only shown once on creation; we store a hash.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)

    # Organization scope
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    # Key identification
    name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # Human-readable name
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # First 8 chars for display
    key_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )  # SHA256 of full key

    # Permissions
    scope: Mapped[APIKeyScope] = mapped_column(
        SQLEnum(
            APIKeyScope,
            name="apikeyscope",
            values_callable=lambda enum: [e.value for e in enum],
        ),
        default=APIKeyScope.FULL,
        nullable=False,
    )

    # Creator tracking
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Status and expiry
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    organization: Mapped["OrganizationModel"] = relationship(  # type: ignore[assignment]
        "OrganizationModel", back_populates="api_keys", lazy="selectin"
    )
    created_by_user: Mapped["UserModel | None"] = relationship(  # type: ignore[assignment]
        "UserModel", back_populates="api_keys", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_api_keys_org_id", "org_id"),
        Index("idx_api_keys_key_hash", "key_hash"),
    )


class QueueSlotModel(Base):
    """Worker slot lease keyed by queue key (model entrypoint)."""

    __tablename__ = "queue_slots"

    queue_key: Mapped[str] = mapped_column(Text, primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "idx_queue_slots_queue_key_locked_until",
            "queue_key",
            "locked_until",
        ),
    )


# =============================================================================
# Helper functions
# =============================================================================


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(
    org_id: str,
    name: str,
    scope: APIKeyScope = APIKeyScope.FULL,
    created_by_user_id: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[APIKeyModel, str]:
    """
    Create a new API key.

    Returns:
        tuple of (APIKeyModel instance, raw key string)

    The raw key is only available at creation time and should be
    shown to the user immediately.
    """
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)

    api_key = APIKeyModel(
        org_id=org_id,
        name=name,
        key_prefix=raw_key[:11],  # "ok_" + first 8 chars
        key_hash=key_hash,
        scope=scope,
        created_by_user_id=created_by_user_id,
        expires_at=expires_at,
    )

    return api_key, raw_key
