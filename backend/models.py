from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import mapped_column as mapped_column  # type: ignore[attr-defined]

# Import shared base from OSS oddish
from oddish.db.models import Base, TimestampedMixin, utcnow

# Re-export API key types and helpers from the shared oddish package so all
# existing ``from models import ...`` call sites keep resolving unchanged.
from oddish.db.models import APIKeyModel, APIKeyScope  # noqa: F401
from oddish.core.api_keys import (  # noqa: F401
    create_api_key, generate_api_key, hash_api_key,
)


def generate_id() -> str:
    """Generate a short unique ID."""
    return str(uuid4())[:8]


# =============================================================================
# Enums
# =============================================================================


class UserRole(str, Enum):
    """User roles within an organization."""

    ADMIN = "admin"  # Can manage users and settings
    MEMBER = "member"  # Can run evals, view results


# =============================================================================
# Cloud Models
# =============================================================================


class OrganizationModel(TimestampedMixin, Base):
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
    # ``APIKeyModel.org_id`` is a plain column in oddish core (no ORM-level
    # ForeignKey, so the standalone OSS server's create_all works), so the join
    # condition can't be inferred and is spelled out here. ``viewonly``: this is
    # a read-only convenience collection; keys are created via
    # ``oddish.core.api_keys``, not by mutating this list.
    api_keys: Mapped[list["APIKeyModel"]] = relationship(  # type: ignore[assignment]
        "APIKeyModel",
        primaryjoin="foreign(APIKeyModel.org_id) == OrganizationModel.id",
        viewonly=True,
        lazy="selectin",
    )


class UserModel(TimestampedMixin, Base):
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
    # Cached dashboard Mine aliases (handles + legacy emails); refreshed lazily.
    attribution_cache: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When true, tasks this user creates default to run_probe=True (auto-probe
    # on submit) without the caller passing --run-probe. Only ever turns the
    # flag ON; an explicit run_probe=True on the submission still wins. Set per
    # user by an operator; there is no UI yet.
    run_probe_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # Relationships
    organization: Mapped["OrganizationModel"] = relationship(  # type: ignore[assignment]
        "OrganizationModel", back_populates="users", lazy="selectin"
    )
    # See ``OrganizationModel.api_keys``: ``APIKeyModel.created_by_user_id`` is a
    # plain core column, so the join is explicit and the collection is viewonly.
    api_keys: Mapped[list["APIKeyModel"]] = relationship(  # type: ignore[assignment]
        "APIKeyModel",
        primaryjoin="foreign(APIKeyModel.created_by_user_id) == UserModel.id",
        viewonly=True,
        lazy="selectin",
    )

    __table_args__ = (
        # A user can only be in one org with one email
        UniqueConstraint("org_id", "email", name="uq_users_org_email"),
        Index("idx_users_org_id", "org_id"),
        Index("idx_users_email", "email"),
        Index("idx_users_github_username", "github_username"),
    )


# =============================================================================
# Chat models
# =============================================================================


# Member names are intentionally lowercase: they mirror the stored string
# values and are referenced as e.g. ChatStatus.active by the ported chat code.
class ChatScopeKind(str, Enum):
    experiment = "experiment"
    task_probes = "task_probes"
    task = "task"
    global_ = "global"


class ChatStatus(str, Enum):
    provisioning = "provisioning"
    active = "active"
    closed = "closed"
    broken = "broken"


class ChatTurnStatus(str, Enum):
    running = "running"
    done = "done"
    failed = "failed"
    canceled = "canceled"


# These chat tables deliberately do not use TimestampedMixin: they need no
# updated_at/deleted_at, and their columns must mirror the raw-SQL migration exactly.
class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_status_last_activity", "status", "last_activity"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Internal READ api-key minted for the global-scope sandbox callback.
    # Intentionally unconstrained (no FK): the key is hard-deleted when the
    # session closes and this id is never read afterward.
    query_api_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    daytona_session_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="cc"
    )  # "cc" is the default Daytona session id used by the chat orchestrator
    claude_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_activity: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChatSessionEvent(Base):
    __tablename__ = "chat_session_events"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "seq", name="uq_chat_session_events_session_seq"
        ),
        Index("ix_chat_session_events_session_seq", "session_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        Index(
            "uq_chat_turns_one_running",
            "session_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_chat_turns_session_seq", "session_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# =============================================================================
# Request idempotency
# =============================================================================


class SubmissionIdempotency(Base):
    """Idempotency record for a side-effecting submission (POST /tasks/sweep).

    Subclasses ``Base`` directly (not ``TimestampedMixin``) and is deliberately
    NOT registered for soft delete: an expired record is hard-deleted so the
    unique slot is freed for a fresh submission with the same key. The record
    stores a fingerprint of the original request (``request_hash``) to reject a
    key replayed with a different body, and the original response
    (``response_json``) to replay on a faithful retry. Records older than their
    ``expires_at`` (24h) are pruned and re-runnable.
    """

    __tablename__ = "submission_idempotency"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    # Tenant scope; combined with route + key for uniqueness so different orgs
    # may reuse the same client-supplied key without colliding.
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Logical endpoint the key was issued against.
    route: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 of the client-supplied Idempotency-Key header.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 fingerprint of the original request body.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Lifecycle: 'in_progress' while the first request runs, 'completed' once the
    # response is stored. Plain text + CHECK (not a PG enum) keeps the migration
    # cleanly reversible.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress"
    )
    # Stored response for replay; NULL until the request completes.
    response_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index(
            "uq_submission_idempotency_org_route_key",
            "org_id",
            "route",
            "key_hash",
            unique=True,
        ),
        # Supports pruning expired records.
        Index("ix_submission_idempotency_expires_at", "expires_at"),
        CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_submission_idempotency_status",
        ),
    )


# ---------------------------------------------------------------------------
# Soft-delete registration
# ---------------------------------------------------------------------------
#
# The session-level filter is installed by ``oddish.db.connection`` and
# only acts on models that have been explicitly registered. The oddish
# core registers its domain models; the cloud layer registers its auth
# models here so the filter covers them too without forcing oddish to
# know about backend-only classes.
from oddish.db.soft_delete import register_soft_delete_models

register_soft_delete_models(OrganizationModel, UserModel, APIKeyModel)


