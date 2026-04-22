from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import DeclarativeBase, mapped_column  # type: ignore[attr-defined]


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
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
    - SUCCESS: Trial executed to completion and produced a result (reward can be any score in [0, 1])
    - FAILED: Trial encountered an execution error (harness failure, API error, timeout, etc.)

    The trial's `reward` field stores the test result separately:
    - reward=1.0: Perfect score / full pass
    - reward=0.0: No credit / full fail
    - 0 < reward < 1: Partial credit
    - reward=None: No test result available (error occurred before/during verification)
    """

    PENDING = "pending"
    QUEUED = "queued"
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


class TrialOrigin(str, Enum):
    """Where a trial's execution happened.

    ``ODDISH`` trials were scheduled and run by Oddish's worker runtime
    (the default, live path).  ``IMPORTED`` trials were executed on an
    external Harbor invocation and uploaded via ``oddish import`` / the
    ``/trials/import/*`` endpoints. Imported trials skip the queue and
    land in a terminal state with the artifacts the client uploaded.
    """

    ODDISH = "oddish"
    IMPORTED = "imported"


class WorkerJobKind(str, Enum):
    """Kind of work represented by a `worker_jobs` row.

    The polymorphism discriminator for the unified queue table. Handlers
    register against a kind; the dispatcher is kind-agnostic.
    """

    TRIAL = "TRIAL"
    ANALYSIS = "ANALYSIS"
    VERDICT = "VERDICT"
    QA_REVIEW = "QA_REVIEW"
    # Expand a task tarball into a per-file S3 tree at
    # ``tasks/{task_id}/v{N}-files/``. Derived cache only; the archive
    # at ``tasks/{task_id}/v{N}/.oddish-task.tar.gz`` remains the
    # canonical, immutable artifact.
    TASK_EXPAND = "TASK_EXPAND"


class WorkerJobStatus(str, Enum):
    """Single state machine for every kind of worker job.

    `BLOCKED` is reserved for future M-of-N dependency gating; v1 keeps
    stage transitions driven by application-level enqueue helpers and
    does not enter BLOCKED.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


# =============================================================================
# SQLAlchemy Models (Database Tables)
# =============================================================================


# Association table: tasks ↔ experiments is many-to-many. A single task can
# belong to several experiments (e.g. the same dataset sweep re-run under
# a new experiment label), while a trial still belongs to exactly one
# experiment via ``TrialModel.experiment_id``.
task_experiments = Table(
    "task_experiments",
    Base.metadata,
    Column(
        "task_id",
        String(64),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    ),
    Index("idx_task_experiments_experiment_id", "experiment_id"),
)


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
        "TaskModel",
        secondary=task_experiments,
        back_populates="experiments",
        lazy="selectin",
        passive_deletes=True,
    )


class TaskModel(Base):
    """Task database model (one Harbor task submission)."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_org_created_at", "org_id", "created_at"),
        Index(
            "idx_tasks_unique_org_name",
            text("COALESCE(org_id, '')"),
            "name",
            unique=True,
        ),
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
    )  # S3 prefix for task files (mirrors latest version)
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Versioning: points to the latest TaskVersionModel row
    current_version_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("task_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

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
    experiments: Mapped[list["ExperimentModel"]] = relationship(  # type: ignore[assignment]
        "ExperimentModel",
        secondary=task_experiments,
        back_populates="tasks",
        lazy="selectin",
    )
    trials: Mapped[list["TrialModel"]] = relationship(  # type: ignore[assignment]
        "TrialModel",
        back_populates="task",
        lazy="selectin",
        passive_deletes=True,
    )
    versions: Mapped[list["TaskVersionModel"]] = relationship(  # type: ignore[assignment]
        "TaskVersionModel",
        back_populates="task",
        lazy="selectin",
        foreign_keys="TaskVersionModel.task_id",
        passive_deletes=True,
    )
    current_version: Mapped["TaskVersionModel | None"] = relationship(  # type: ignore[assignment]
        "TaskVersionModel",
        foreign_keys=[current_version_id],
        lazy="selectin",
        uselist=False,
    )


class TaskVersionModel(Base):
    """Immutable snapshot of a task's content at a point in time.

    Each re-upload of a task bundle creates a new row.  Trials reference the
    specific version they ran against via ``task_version_id``.
    """

    __tablename__ = "task_versions"
    __table_args__ = (
        Index(
            "idx_task_versions_task_id_version",
            "task_id",
            "version",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_path: Mapped[str] = mapped_column(Text, nullable=False)
    task_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Expansion bookkeeping: set when a ``TASK_EXPAND`` worker job has
    # successfully materialized the per-file tree under
    # ``tasks/{task_id}/v{N}-files/``. Readers check the sibling
    # ``.oddish-manifest.json`` sentinel in S3 directly, so these columns
    # are observability / admin-backfill state rather than a required
    # fast-path signal.
    expanded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expanded_manifest_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    task: Mapped["TaskModel"] = relationship(  # type: ignore[assignment]
        "TaskModel",
        back_populates="versions",
        foreign_keys=[task_id],
        lazy="selectin",
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
    task_version_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("task_versions.id", ondelete="SET NULL"), nullable=True
    )
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    queue_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timeout_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Harbor passthrough config (agent env/kwargs, verifier, environment resources)
    harbor_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Status
    status: Mapped[TrialStatus] = mapped_column(
        SQLEnum(TrialStatus), default=TrialStatus.PENDING, nullable=False
    )
    # Whether this trial ran on Oddish's worker runtime or was uploaded
    # from an external Harbor invocation via ``oddish import``.
    #
    # ``values_callable`` is required because SQLAlchemy's default enum
    # lookup uses *member names* (``ODDISH`` / ``IMPORTED``), while the
    # migration stores the lowercase *values* (``oddish`` / ``imported``)
    # to match the CHECK constraint and ``server_default``. Without it,
    # reads fail with ``LookupError: 'oddish' is not among the defined
    # enum values``. Mirrors ``WorkerJobKind`` / ``WorkerJobStatus``.
    origin: Mapped[TrialOrigin] = mapped_column(
        SQLEnum(
            TrialOrigin,
            name="trial_origin",
            native_enum=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=TrialOrigin.ODDISH,
        nullable=False,
        server_default=TrialOrigin.ODDISH.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6, nullable=False)

    # Harbor execution stage (from lifecycle hooks)
    harbor_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Current execution claim metadata
    current_worker_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True, index=True
    )
    current_queue_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Timestamp set when stale-heartbeat cleanup killed this trial. Kept
    # separate from heartbeat_at so the worker's last successful heartbeat
    # is preserved for post-mortem analysis (previously we overwrote
    # heartbeat_at on cleanup, destroying that evidence).
    stale_reaped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Observability for heartbeat write failures. Populated by the worker's
    # heartbeat loop whenever a DB write raises. Lets operators distinguish
    # "worker process died" from "DB/pooler was unreachable" after a
    # stale-heartbeat reap without digging through Modal logs.
    heartbeat_failure_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    last_heartbeat_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    reward: Mapped[float | None] = mapped_column(Float, nullable=True)
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
        Index("idx_trials_task_id", "task_id"),
        Index("idx_trials_task_version_id", "task_version_id"),
        # Display / API filter path. Claim/stale-reap indexes on
        # trials were retired in the ``worker_jobs`` refactor --
        # scheduling queries now hit ``idx_worker_jobs_claim`` and
        # ``idx_worker_jobs_heartbeat`` instead.
        Index("idx_trials_status", "status"),
        # Supports "imported only" filters without scanning every
        # oddish-origin row. Kept partial because oddish-origin is the
        # common case and indexing it gains nothing.
        Index(
            "idx_trials_origin",
            "origin",
            postgresql_where=text("origin <> 'oddish'"),
        ),
        # Composite index for efficient queue stats aggregation (no JOIN needed)
        Index("idx_trials_org_provider_status", "org_id", "provider", "status"),
        Index("idx_trials_org_queue_key_status", "org_id", "queue_key", "status"),
        Index(
            "idx_trials_org_experiment_created_at",
            "org_id",
            "experiment_id",
            "created_at",
        ),
        Index(
            "idx_trials_dashboard_usage",
            "org_id",
            "created_at",
            "model",
            "provider",
        ),
    )


class QueueSlotModel(Base):
    """Worker slot lease keyed by queue key."""

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


class WorkerJobModel(Base):
    """Unified queue row for every kind of compute work.

    Phase A of the `worker_jobs` migration introduces the table with no
    readers or writers. The dispatcher, claim path, cleanup sweep, and
    cancel path will cut over to this table in later phases; see
    ``.cursor/plans/unified_worker_jobs_table.plan.md``.

    The source-of-truth rule is: this table is authoritative for
    *scheduling state* only. Domain tables (``trials`` / ``tasks``)
    remain authoritative for domain state (``trials.status``,
    ``trials.harbor_stage``, ``trials.reward``, ``tasks.verdict`` ...).
    Harbor lifecycle hooks keep writing domain-state columns during
    execution; handlers mirror the terminal state back on completion.
    """

    __tablename__ = "worker_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)

    kind: Mapped[WorkerJobKind] = mapped_column(
        SQLEnum(
            WorkerJobKind,
            name="worker_job_kind",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    status: Mapped[WorkerJobStatus] = mapped_column(
        SQLEnum(
            WorkerJobStatus,
            name="worker_job_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=WorkerJobStatus.QUEUED,
        server_default="QUEUED",
    )

    queue_key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )

    # Denormalized pointer to the domain row this job is about. Not a
    # foreign key because different `kind`s target different tables
    # (TRIAL/ANALYSIS -> trials, VERDICT -> tasks, QA_REVIEW -> trials,
    # future free-floating jobs -> null). Kept as a pair of TEXT
    # columns so dispatcher and reaper reads stay join-free.
    subject_table: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Application-level parent pointer (see plan: "Dependencies").
    # v1 uses this only for audit trails; stage transitions are still
    # driven by enqueue helpers rather than a BLOCKED-state gate.
    parent_job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("worker_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )

    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6, server_default="6"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("NOW()"),
    )

    current_worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_queue_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modal_function_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stale_reaped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_heartbeat_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Small per-kind result (a few KB max). Large blobs like the full
    # classification / verdict stay on the domain table.
    result_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    org_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_worker_jobs_claim",
            "queue_key",
            "priority",
            "available_after",
            "created_at",
            postgresql_where=text("status IN ('QUEUED', 'RETRYING')"),
        ),
        Index(
            "idx_worker_jobs_heartbeat",
            "status",
            "heartbeat_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index(
            "idx_worker_jobs_subject",
            "subject_table",
            "subject_id",
        ),
        Index(
            "idx_worker_jobs_parent",
            "parent_job_id",
            postgresql_where=text("parent_job_id IS NOT NULL"),
        ),
        Index(
            "idx_worker_jobs_org",
            "org_id",
            "status",
            postgresql_where=text("org_id IS NOT NULL"),
        ),
    )
