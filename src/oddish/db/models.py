from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import DeclarativeBase, mapped_column  # type: ignore[attr-defined]


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base with common fields for all models."""

    # All models inherit these fields
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def generate_id() -> str:
    """Generate a short unique ID."""
    return str(uuid4())[:8]


# =============================================================================
# Enums
# =============================================================================


class TaskStatus(str, Enum):
    """Task execution status - tracks pipeline stage."""

    PENDING = "pending"  # Task created, trials not yet started
    RUNNING = "running"  # Trials are running
    ANALYZING = "analyzing"  # All trials done, analyses running
    VERDICT_PENDING = "verdict_pending"  # All analyses done, verdict running
    COMPLETED = "completed"  # All stages complete
    FAILED = "failed"  # Terminal failure


class JobStatus(str, Enum):
    """Execution status for trials, analyses, and verdicts.

    For trials specifically:
    - SUCCESS: Trial executed to completion and produced a result (reward can be 0 or 1)
    - FAILED: Trial encountered an execution error (harness failure, API error, timeout, etc.)

    The trial's `reward` field stores the test result separately:
    - reward=1: Test passed (agent successfully fixed the bug)
    - reward=0: Test failed (agent did not fix the bug)
    - reward=None: No test result available (error occurred before/during verification)
    """

    PENDING = "pending"
    QUEUED = "queued"  # Enqueued in PGQueuer
    RUNNING = "running"
    SUCCESS = "success"  # Execution completed (regardless of test result)
    FAILED = "failed"  # Execution error (harness/infrastructure failure)
    RETRYING = "retrying"  # Only used by trials


# Aliases for backwards compatibility and clarity
TrialStatus = JobStatus
AnalysisStatus = JobStatus
VerdictStatus = JobStatus


class Priority(str, Enum):
    """Task priority levels."""

    HIGH = "high"
    LOW = "low"


# =============================================================================
# SQLAlchemy Models (Database Tables)
# =============================================================================


class ExperimentModel(Base):
    """Experiment database model (grouping for tasks)."""

    __tablename__ = "experiments"
    __table_args__ = (
        Index("idx_experiments_public_token", "public_token", unique=True),
    )

    # Override id to add auto-generation
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # -------------------------------------------------------------------------
    # Cloud-ready column (denormalized for efficient org-scoped queries)
    # -------------------------------------------------------------------------
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Public sharing (nullable until published)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tasks: Mapped[list["TaskModel"]] = relationship(  # type: ignore[assignment]
        "TaskModel", back_populates="experiment", lazy="selectin"
    )


class TaskModel(Base):
    """Task database model (one Harbor task submission)."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_org_created_at", "org_id", "created_at"),
        Index("idx_tasks_experiment_id", "experiment_id"),
    )

    # Override id to add auto-generation
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # -------------------------------------------------------------------------
    # Cloud-ready columns (no FK constraints in OSS)
    # In OSS: these are just nullable strings, ignored or used for basic grouping
    # In Cloud: FK constraints are added via migration to enforce relationships
    # -------------------------------------------------------------------------
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[Priority] = mapped_column(
        SQLEnum(Priority), default=Priority.LOW, nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )
    task_path: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # Original local path or task name
    task_s3_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # S3 prefix for task files
    experiment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Analysis settings
    run_analysis: Mapped[bool] = mapped_column(default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Verdict data (consolidated LLM verdict for this task)
    verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verdict_status: Mapped[VerdictStatus | None] = mapped_column(
        SQLEnum(VerdictStatus), nullable=True
    )
    verdict_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verdict_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    experiment: Mapped["ExperimentModel"] = relationship(  # type: ignore[assignment]
        "ExperimentModel", back_populates="tasks", lazy="selectin"
    )
    trials: Mapped[list["TrialModel"]] = relationship(  # type: ignore[assignment]
        "TrialModel", back_populates="task", lazy="selectin"
    )


class TrialModel(Base):
    """Trial database model."""

    __tablename__ = "trials"

    # Override id: Stable, human-friendly ID set manually as "{task_id}-{index}"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )

    # -------------------------------------------------------------------------
    # Cloud-ready column (denormalized for efficient org-scoped queries)
    # Backfilled from task.org_id - eliminates JOIN in queue stats queries
    # -------------------------------------------------------------------------
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Idempotency key for preventing duplicate processing of retried jobs
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    # Trial spec
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=60)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Harbor passthrough config (agent env/kwargs, verifier, environment resources)
    harbor_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Status
    status: Mapped[TrialStatus] = mapped_column(
        SQLEnum(TrialStatus), default=TrialStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6, nullable=False)

    # Harbor execution stage (from lifecycle hooks)
    harbor_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Results
    reward: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    harbor_result_path: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Legacy: local path
    trial_s3_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # S3 prefix for trial results/logs
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Token usage & cost (extracted from Harbor's AgentContext)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Per-phase timing breakdown (from Harbor's TrialResult TimingInfo)
    phase_timing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Whether an ATIF trajectory file exists for this trial
    has_trajectory: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # Analysis data (LLM analysis of this trial)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis_status: Mapped[AnalysisStatus | None] = mapped_column(
        SQLEnum(AnalysisStatus), nullable=True
    )
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    analysis_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    task: Mapped["TaskModel"] = relationship(  # type: ignore[assignment]
        "TaskModel", back_populates="trials", lazy="selectin"
    )

    __table_args__ = (
        # Composite index for efficient trial claiming queries
        Index("idx_trials_claimable", "status", "provider", "next_retry_at"),
        Index("idx_trials_task_id", "task_id"),
        Index("idx_trials_status", "status"),
        # Composite index for efficient queue stats aggregation (no JOIN needed)
        Index("idx_trials_org_provider_status", "org_id", "provider", "status"),
    )
