from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    and_,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.ext.asyncio import AsyncAttrs  # type: ignore[attr-defined]
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import DeclarativeBase, mapped_column  # type: ignore[attr-defined]


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    """Bare declarative base. Use :class:`TimestampedMixin` to opt into
    the standard ``id`` / ``created_at`` / ``updated_at`` / ``deleted_at``
    fields.

    Tables that don't fit that shape (e.g. ``QueueSlotModel`` with a
    composite PK) subclass ``Base`` directly without the mixin.
    """


class TimestampedMixin:
    """Common fields most domain tables share.

    ``deleted_at`` is the soft-delete tombstone column. The session-level
    filter in :mod:`oddish.db.soft_delete` auto-applies
    ``WHERE deleted_at IS NULL`` to every ORM SELECT / UPDATE / DELETE on
    every mapped class registered via ``register_soft_delete_models``,
    so most call sites don't need to filter explicitly. Use
    ``.execution_options(include_deleted=True)`` to opt out per statement
    (admin tooling, restore flows).
    """

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

    # TODO(deprecate-pending): PENDING is a vestigial state -- no code path
    # ever assigns it at runtime (trials are created as QUEUED; analyses
    # and verdicts start NULL and jump straight to QUEUED). It only
    # survives as the default on ``trials.status`` and as defensive
    # membership in ``.in_([PENDING, QUEUED, ...])`` checks. The FE now
    # folds PENDING into "queued" visually (see
    # ``frontend/src/lib/status-config.ts:getMatrixStatus``). Follow-up:
    # stop writing PENDING entirely, backfill any legacy rows to QUEUED,
    # drop the enum value from this class, and then ``ALTER TYPE
    # jobstatus DROP VALUE 'PENDING'`` (Postgres 17+) or swap to a fresh
    # enum type on older servers.
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"  # Execution completed (regardless of test result)
    FAILED = "failed"  # Execution error (harness/infrastructure failure)
    RETRYING = "retrying"  # Only used by trials
    # Trials only: the trial never ran because the baseline gate cancelled it
    # (its nop/oracle baselines didn't validate the task). Terminal and its own
    # bucket (not a failure), but counted as a non-pass in pass-rate denominators
    # and toward "done", like a harness error.
    SKIPPED = "skipped"


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
    # The single task-level trajectory-analysis (QA) job: it classifies every
    # trial's trajectory and then synthesizes the task verdict in one job.
    QA = "QA"
    # Legacy kinds. Trajectory analysis used to be a per-trial ``ANALYSIS`` job
    # plus a separate per-task ``VERDICT`` job; both collapsed into ``QA``.
    # Nothing enqueues these anymore. They are kept as enum members so the
    # native ``worker_job_kind`` Postgres type (created from this enum) still
    # carries the values that historical migrations / rows reference, and so
    # any row in flight across the deploy can drain. ``qa02`` repoints existing
    # ``VERDICT`` rows to ``QA``.
    VERDICT = "VERDICT"
    ANALYSIS = "ANALYSIS"
    QA_REVIEW = "QA_REVIEW"
    # Expand a task tarball into a per-file S3 tree at
    # ``tasks/{task_id}/v{N}-files/``. Derived cache only; the archive
    # at ``tasks/{task_id}/v{N}/.oddish-task.tar.gz`` remains the
    # canonical, immutable artifact.
    TASK_EXPAND = "TASK_EXPAND"
    # Recompute one or more rows' projected ``effective_tag_ids`` arrays
    # from the truth tables (tags / tag_assignments / tag_exclusions /
    # task_experiments). Idempotent and order-independent: every run
    # rebuilds from source rather than applying a delta. Sibling-enqueued
    # by every tag write in the same transaction.
    TAG_PROJECT = "TAG_PROJECT"
    # Generate a cross-experiment analysis analyzer: fans out per-trial Haiku
    # findings and reduces them into four narrative sections. Runs on the QA
    # queue; handled by AnalyzerJobHandler.
    ANALYZER = "ANALYZER"
    # Execute one declaratively persisted AnalyzerRunModel. The handler
    # reconstructs an AnalyzerBlock; the block owns its LLM/sandbox lifecycle.
    ANALYZER_BLOCK = "ANALYZER_BLOCK"


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


class TagState(str, Enum):
    """Lifecycle state for a tag definition."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    MERGED = "MERGED"
    DELETED = "DELETED"


class TagVisibility(str, Enum):
    """Whether a tag is visible on public share/dataset pages."""

    PRIVATE = "PRIVATE"
    PUBLIC = "PUBLIC"


class TagAssignmentScope(str, Enum):
    """What kind of target a tag assignment is bound to."""

    VERSION = "VERSION"
    TASK = "TASK"
    EXPERIMENT = "EXPERIMENT"


class TagAssignmentState(str, Enum):
    """Lifecycle state for an individual tag assignment row."""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class TagAssignmentSource(str, Enum):
    """Where an assignment came from (direct vs experiment-derived)."""

    DIRECT = "DIRECT"
    EXPERIMENT_SNAPSHOT = "EXPERIMENT_SNAPSHOT"
    EXPERIMENT_LIVING = "EXPERIMENT_LIVING"


class TagGrantPrincipal(str, Enum):
    USER = "USER"
    ALL_MEMBERS = "ALL_MEMBERS"


class TagGrantCapability(str, Enum):
    RENAME = "RENAME"
    MERGE = "MERGE"
    DELETE = "DELETE"
    EDIT = "EDIT"


class TagEventAction(str, Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    RENAME = "RENAME"
    ARCHIVE = "ARCHIVE"
    UNARCHIVE = "UNARCHIVE"
    MERGE = "MERGE"
    DELETE = "DELETE"
    APPLY = "APPLY"
    REMOVE = "REMOVE"
    EXCLUDE = "EXCLUDE"
    UNEXCLUDE = "UNEXCLUDE"
    GRANT = "GRANT"
    REVOKE = "REVOKE"
    SET_VISIBILITY = "SET_VISIBILITY"
    POLICY_CHANGE = "POLICY_CHANGE"


class TagEventActor(str, Enum):
    USER = "USER"
    API_KEY = "API_KEY"
    SYSTEM = "SYSTEM"


class TagEventSource(str, Enum):
    UI = "UI"
    API = "API"
    CLI = "CLI"
    INHERITANCE = "INHERITANCE"
    RECONCILER = "RECONCILER"


class SavedTagFilterVisibility(str, Enum):
    PRIVATE = "PRIVATE"
    ORG = "ORG"


class TagPolicyWhoCanCreate(str, Enum):
    ANY_MEMBER = "ANY_MEMBER"
    ADMIN_ONLY = "ADMIN_ONLY"


class TagPolicyProfanityMode(str, Enum):
    ENFORCE = "ENFORCE"
    REPORT = "REPORT"
    OFF = "OFF"


class APIKeyScope(str, Enum):
    """API key permission scopes."""

    FULL = "full"  # All operations (tasks, trials, admin)
    TASKS = "tasks"  # Create/view tasks and trials only
    READ = "read"  # Read-only access


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
        String(128),
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
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Index("idx_task_experiments_experiment_id", "experiment_id"),
    Index("idx_task_experiments_experiment_task", "experiment_id", "task_id"),
)


# Association table for read-only "collection" experiments: gathers existing
# trials (from their home experiments) into a new experiment for dashboard
# viewing, without moving the trials.
experiment_trials = Table(
    "experiment_trials",
    Base.metadata,
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "trial_id",
        String(160),
        ForeignKey("trials.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    ),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Index("idx_experiment_trials_trial_id", "trial_id"),
)


# Association table: analyzers ↔ experiments. A analyzer analyzes trajectories
# across N experiments; membership is additive and soft-deletable, mirroring
# ``experiment_trials``.
analyzer_experiments = Table(
    "analyzer_experiments",
    Base.metadata,
    Column(
        "analyzer_id",
        String(64),
        ForeignKey("analyzers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "experiment_id",
        String(64),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Index("idx_analyzer_experiments_experiment_id", "experiment_id"),
)


class ExperimentModel(TimestampedMixin, Base):
    """Experiment database model (grouping for tasks)."""

    __tablename__ = "experiments"
    __table_args__ = (
        Index("idx_experiments_public_token", "public_token", unique=True),
        # Backs the dashboard "recent experiments" sort. Partial on
        # ``deleted_at IS NULL`` so the soft-delete listener can ride
        # the index. ``DESC NULLS LAST`` matches the SQL ORDER BY.
        Index(
            "idx_experiments_org_last_activity_live",
            "org_id",
            "last_activity_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_experiments_org_owner_user_live",
            "org_id",
            "owner_user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Mine page fast path: seek by ``(org_id, owner_user_id)`` and
        # walk the index in ``last_activity_at DESC NULLS LAST, id ASC``
        # order so the dashboard query doesn't pay a separate sort.
        Index(
            "idx_experiments_org_owner_activity_live",
            "org_id",
            "owner_user_id",
            text("last_activity_at DESC NULLS LAST"),
            text("id ASC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Mirror the partial-unique pattern used on ``tasks`` so a
        # soft-deleted experiment doesn't take its name slot with it.
        # Experiments don't currently have a name uniqueness constraint,
        # but new code that adds one should follow the same convention.
    )

    # Override id to add auto-generation
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # -------------------------------------------------------------------------
    # Cloud-ready column (denormalized for efficient org-scoped queries)
    # -------------------------------------------------------------------------
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Denormalized "last activity" timestamp for the dashboard recent
    # experiments sort. Updated best-effort by ``create_task``, the
    # task version insert path, and trial inserts/finishes. Falling
    # back to ``NULL`` is fine -- the dashboard query treats NULL as
    # "no activity" and orders it last. A periodic reconciliation job
    # in the cleanup sweep (``oddish.workers.queue.cleanup``) refreshes
    # any drift from missed write-path updates.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Primary owner for dashboard Mine filter (stamped from the first task submit).
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Per-experiment provenance, stamped set-once from the creating run's
    # submitter. Unlike the shared, mutable ``task.link`` / ``task.tags`` (which
    # any later run of a shared task overwrites), these belong to THIS
    # experiment and never change once set, so the experiment always shows the
    # PR/owner it was created for. ``owner`` is a display string (a GitHub
    # handle or a plain username); distinct from ``owner_user_id`` (the internal
    # user id used only by the Mine filter).
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Public sharing (nullable until published)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Read-only "collection" experiment: gathers existing trials from other
    # experiments for dashboard viewing (see ``experiment_trials``).
    is_collection: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # User-authored markdown description shown in the experiment header.
    # Nullable; ``None``/blank means "no description".
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Migration markers (Sauron->Oddish import). NULL for normal rows.
    # ``imported_at`` = when this row was created by the legacy importer ->
    # clean audit/rollback (WHERE imported_at IS NOT NULL). ``orig_s3_src`` =
    # the immutable Sauron run-root S3 path this experiment came from (encodes
    # base/pr/run); anchor for later duplicate reconciliation.
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    orig_s3_src: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ``lazy="select"`` (the default): no production read path actually
    # touches ``experiment.tasks``. Loading an experiment used to fan
    # out into a task fetch via ``task_experiments`` on every access;
    # callers that genuinely need the task list should add
    # ``selectinload(ExperimentModel.tasks)`` explicitly.
    tasks: Mapped[list["TaskModel"]] = relationship(  # type: ignore[assignment]
        "TaskModel",
        secondary=task_experiments,
        primaryjoin=lambda: and_(
            ExperimentModel.id == task_experiments.c.experiment_id,
            task_experiments.c.deleted_at.is_(None),
        ),
        secondaryjoin=lambda: TaskModel.id == task_experiments.c.task_id,
        back_populates="experiments",
        passive_deletes=True,
    )


class AnalyzerModel(TimestampedMixin, Base):
    """Cross-experiment trajectory-analysis analyzer.

    Inherits from experiments in the domain sense: a analyzer references N
    experiments (via ``analyzer_experiments``), gathers their trials, and rolls
    the per-trial subanalysis up one tier into four narrative sections. The
    section bodies are markdown with inline ``/tasks/{task_id}/probe/{trial_id}``
    deep links.
    """

    __tablename__ = "analyzers"
    __table_args__ = (
        Index(
            "idx_analyzers_org_created_live",
            "org_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_analyzers_org_owner_user_live",
            "org_id",
            "owner_user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        SQLEnum(JobStatus, name="jobstatus", create_type=False),
        default=JobStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    bad_failure_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    good_failure_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    universal_capabilities_content: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    headroom_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-model insights payload. NULL = pre-analyzers_008 report; the report UI
    # falls back to the four-section render on NULL, so do not default this to {}.
    by_model: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # The reduce-stage prompt that produced the section bodies above; persisted
    # for debugging/reproducibility. NULL for zero-failure analyzers (no reduce).
    reduce_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    num_trials: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_bad_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_good_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Additive: per-subcategory counts (1a/1b, 3a/3b/3c, emergent) for FE chips.
    breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Per-trial findings from the map phase. NULL = analyzed before findings
    # were persisted; [] = analyzed, no failures found. Not the same thing.
    findings: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Per-task roster of models that ran it, including those that passed --
    # the Task Construction denominator can't come from findings (failures only).
    models_by_task: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Opt-in (set at create time): when true, the worker uploads the per-trial
    # findings+subanalyses to S3 (analyzers/{id}/trial_analyses.json).
    save_trial_analyses: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AnalyzerBlockModel(TimestampedMixin, Base):
    """One run of a single composable analyzer block.

    Standalone primitive (not part of ``run_analyzer_generation_job``): many
    blocks chain arbitrarily in test scripts. ``type`` / ``llm_client_type`` are
    the ``.value`` of the ``AnalyzerType`` / ``LLMClientType`` enums defined in
    ``backend/api/services`` -- stored as plain strings so this module stays free
    of any backend-package dependency. Raw streamed output lives in S3 at
    ``{key_prefix}/{id}``; ``output`` here is the accumulated/parsed result.
    """

    __tablename__ = "analyzer_blocks"
    __table_args__ = (
        Index(
            "idx_analyzer_blocks_analyzer_id_live",
            "analyzer_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    analyzer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Task-level QA blocks use this explicit subject link. ``analyzer_id`` is
    # reserved for the existing AnalyzerModel/report association.
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    llm_client_type: Mapped[str] = mapped_column(String(64), nullable=False)

    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which prompts row produced this block. prompt_key/prompt_version alone
    # cannot attribute usage once the same kind exists at several scopes.
    prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # input/output are arbitrary JSON (the block's I/O are typed ``any``).
    input: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        PGEnum(JobStatus, name="jobstatus", create_type=False),
        default=JobStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ``metadata`` is reserved on the declarative Base, so the attribute is
    # ``block_metadata`` while the DB column is literally named ``metadata``.
    block_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )


class AnalyzerRunModel(TimestampedMixin, Base):
    """Lineage for one execution of one analyzer prompt version."""

    __tablename__ = "analyzer_runs"
    __table_args__ = (
        Index(
            "uq_analyzer_runs_assignment_event",
            "qa_assignment_id",
            "stage_event_key",
            unique=True,
            postgresql_where=text("qa_assignment_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prompt_version_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False, index=True
    )
    analyzer_block_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_client_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        PGEnum(JobStatus, name="jobstatus", create_type=False), nullable=False
    )
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # NULL for ad-hoc `oddish qa` runs, set for assignment-driven ones. The
    # partial UNIQUE on (qa_assignment_id, stage_event_key) then makes "run this
    # assignment at most once per event" a database invariant, while leaving
    # ad-hoc runs exempt.
    qa_assignment_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("qa_assignments.id", ondelete="SET NULL"), nullable=True
    )
    stage_event_key: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TaskModel(TimestampedMixin, Base):
    """Task database model (one Harbor task submission)."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_org_created_at", "org_id", "created_at"),
        # Partial mirror of ``idx_tasks_org_created_at`` that matches
        # the ``deleted_at IS NULL`` predicate the soft-delete listener
        # appends to every read. Lets the dashboard recent-tasks list
        # and experiment task list ordering use a tight index scan
        # instead of filtering after a wider range scan.
        Index(
            "idx_tasks_org_created_at_live",
            "org_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Soft-deleted rows are excluded so a user can reuse a task name
        # after deletion without hitting a "ghost" tombstone. The
        # auto-filter in :mod:`oddish.db.soft_delete` keeps reads of those
        # rows hidden from normal ORM traffic.
        Index(
            "idx_tasks_unique_org_name",
            text("COALESCE(org_id, '')"),
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Attribution discovery scan + legacy Mine EXISTS fallback:
        # who created this task by Clerk user id? Partial on
        # ``created_by_user_id IS NOT NULL`` keeps the index tight,
        # matching the discovery query predicate exactly.
        Index(
            "idx_tasks_org_created_by_live",
            "org_id",
            "created_by_user_id",
            postgresql_where=text(
                "deleted_at IS NULL AND created_by_user_id IS NOT NULL"
            ),
        ),
        # Legacy Mine fallback: functional index on case-insensitive
        # ``user`` column so the EXISTS subquery rides an index for
        # the Harbor-submitter handle branch of attribution.
        Index(
            "idx_tasks_org_lower_user_live",
            "org_id",
            text('lower("user")'),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Legacy Mine fallback: functional index on the GitHub
        # username carried in the ``tags`` JSONB blob so the EXISTS
        # subquery rides an index for the GitHub-identity branch.
        Index(
            "idx_tasks_org_lower_github_tag_live",
            "org_id",
            text("lower((tags ->> 'github_username'))"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    # Override id to add auto-generation
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # -------------------------------------------------------------------------
    # Cloud-ready columns (no FK constraints in OSS)
    # In OSS: these are just nullable strings, ignored or used for basic grouping
    # In Cloud: FK constraints are added via migration to enforce relationships
    # -------------------------------------------------------------------------
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Audit-only: API key that submitted this task (NULL for JWT/OSS). Billing
    # still follows trials.billed_user_id, never this.
    api_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
    # Materialized read projection — see `oddish.core.tags_projection`.
    effective_tag_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    current_version_tag_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    link: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Versioning: points to the latest TaskVersionModel row
    current_version_id: Mapped[str | None] = mapped_column(
        String(160),
        ForeignKey("task_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    # Analysis settings
    run_analysis: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Auto-probe opt-in: when True, sweeps on this task enqueue a probe trial
    # for the task's current version. Off by default (probes are opt-in).
    run_probe: Mapped[bool] = mapped_column(default=False, nullable=False)

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

    # Migration provenance: set when created by the Sauron->Oddish importer,
    # NULL otherwise. Rich provenance lives in ``tags``; this is the clean
    # audit/rollback marker (WHERE imported_at IS NOT NULL).
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    experiments: Mapped[list["ExperimentModel"]] = relationship(  # type: ignore[assignment]
        "ExperimentModel",
        secondary=task_experiments,
        primaryjoin=lambda: and_(
            TaskModel.id == task_experiments.c.task_id,
            task_experiments.c.deleted_at.is_(None),
        ),
        secondaryjoin=lambda: ExperimentModel.id == task_experiments.c.experiment_id,
        back_populates="tasks",
        lazy="selectin",
    )
    trials: Mapped[list["TrialModel"]] = relationship(  # type: ignore[assignment]
        "TrialModel",
        back_populates="task",
        lazy="selectin",
        passive_deletes=True,
    )
    # ``lazy="select"``: only the explicit ``list_task_versions_core``
    # path actually wants the full version history, and it already
    # issues its own ``SELECT TaskVersionModel WHERE task_id = ...``.
    # Eager-loading on every TaskModel fetch was charging every
    # dashboard / experiment / files endpoint for a fan-out they did
    # not consume.
    versions: Mapped[list["TaskVersionModel"]] = relationship(  # type: ignore[assignment]
        "TaskVersionModel",
        back_populates="task",
        foreign_keys="TaskVersionModel.task_id",
        passive_deletes=True,
    )
    # ``lazy="select"``: only the file-serving routes
    # (``list_task_files`` / ``get_task_file_content`` and their public
    # mirrors) read ``task.current_version.version``. Those call sites
    # add ``selectinload(TaskModel.current_version)`` themselves, so the
    # rest of the codebase stops paying for an extra round trip per
    # TaskModel load.
    current_version: Mapped["TaskVersionModel | None"] = relationship(  # type: ignore[assignment]
        "TaskVersionModel",
        foreign_keys=[current_version_id],
        uselist=False,
    )


class TaskVersionModel(TimestampedMixin, Base):
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

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
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
    effective_tag_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )

    # Pre-trial QA analysis (task-source audit; runs once per version since
    # each version is a distinct source snapshot to audit)
    pre_trial: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pre_trial_status: Mapped[VerdictStatus | None] = mapped_column(
        SQLEnum(VerdictStatus), nullable=True
    )
    pre_trial_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_trial_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pre_trial_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    task: Mapped["TaskModel"] = relationship(  # type: ignore[assignment]
        "TaskModel",
        back_populates="versions",
        foreign_keys=[task_id],
        lazy="selectin",
    )


class TrialModel(TimestampedMixin, Base):
    """Trial database model."""

    __tablename__ = "trials"

    # Override id: Stable, human-friendly ID set manually as "{task_id}-{index}"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_version_id: Mapped[str | None] = mapped_column(
        String(160), ForeignKey("task_versions.id", ondelete="SET NULL"), nullable=True
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

    billed_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    # Concrete Harbor commit SHA this trial executed against (denormalized,
    # indexed projection of harbor_config["resolved_sha"]; stamped at creation).
    harbor_sha: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Derived, indexed projection of ``harbor_config["mode"] == "probe"`` so
    # probe runs can be filtered server-side. Source of truth stays in
    # harbor_config; this is set at trial creation in queue.py.
    is_probe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), index=True
    )

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

    # Token usage, steps & cost (extracted from Harbor's AgentContext / trajectory)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trajectory_duration_seconds: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    total_tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # SHA-256 of the platform provider API key this trial ran on, stamped at
    # settlement (forward-only; NULL for pre-rollout / unresolved keys). Matched
    # against ``cost_excluded_llm_keys`` to drop sponsored/free spend from cost
    # accounting -- see ``oddish.core.cost_basis.first_party_spend_filter``.
    llm_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Per-phase timing breakdown (from Harbor's TrialResult TimingInfo)
    phase_timing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Whether an ATIF trajectory file exists for this trial
    has_trajectory: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # LLM-generated summary of the trajectory; populated lazily on first
    # request to GET /trials/{id}/trajectory/summary. Replaces the prior
    # S3-cached `agent/trajectory_summary.json` sibling file.
    trajectory_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

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
    # The analyzer's live event log for the current/most recent analysis
    # run. Written by the QA worker every few seconds so the UI can show
    # what the analyzer is doing. One short line per event, so it stays small.
    analysis_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Immutable-trial rerun pointer. When a user retries a trial we
    # don't reset this row; instead we insert a fresh trial that copies
    # the spec, and set this column on the old row to point at the new
    # one. UI listings and pipeline counts filter on
    # ``superseded_by_trial_id IS NULL`` so superseded attempts stay in
    # the DB (for history / direct deep-links) but stop cluttering
    # default views, S3 file viewers, and verdict aggregation.
    superseded_by_trial_id: Mapped[str | None] = mapped_column(
        String(160),
        ForeignKey("trials.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Migration markers: set when created by the Sauron->Oddish importer, NULL
    # otherwise. ``imported_at`` is the clean audit/rollback marker; the source
    # tag lives in ``harbor_config``. ``orig_s3_src`` is the IMMUTABLE Sauron
    # attempt-prefix this trial came from -- distinct from ``trial_s3_key``
    # (the mutable artifact-serving location), so the source survives any later
    # artifact copy. Anchor for duplicate reconciliation.
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    orig_s3_src: Mapped[str | None] = mapped_column(Text, nullable=True)

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
            "idx_trials_org_billed_user_finished",
            "org_id",
            "billed_user_id",
            "finished_at",
        ),
        # Org-wide quota SUMs: sum_org_cost_usd seeks (org_id, finished_at >
        # period_start) and org_inflight_reserved_usd seeks (org_id,
        # finished_at IS NULL). The per-user index above can't seek finished_at
        # (it sits behind an unconstrained billed_user_id). Not partial: the
        # settled sum counts soft-deleted rows (include_deleted=True).
        Index(
            "idx_trials_org_finished_at",
            "org_id",
            "finished_at",
        ),
        Index(
            "idx_trials_org_experiment_created_at",
            "org_id",
            "experiment_id",
            "created_at",
        ),
        Index(
            "idx_trials_experiment_task_version",
            "experiment_id",
            "task_id",
            "task_version_id",
        ),
        Index(
            "idx_trials_dashboard_usage",
            "org_id",
            "created_at",
            "model",
            "provider",
        ),
        # Partial index that supports the default "non-superseded only"
        # filter on hot list/aggregation paths without indexing every
        # row (the superseded set is small relative to total trials).
        Index(
            "idx_trials_superseded_by",
            "superseded_by_trial_id",
            postgresql_where=text("superseded_by_trial_id IS NOT NULL"),
        ),
        # Live + non-superseded composite for the dashboard experiment
        # aggregation and experiment-scoped trial listings. Matches
        # both predicates the soft-delete listener and the rerun-history
        # collapse always pair.
        Index(
            "idx_trials_live_org_experiment_created",
            "org_id",
            "experiment_id",
            "created_at",
            postgresql_where=text(
                "deleted_at IS NULL AND superseded_by_trial_id IS NULL"
            ),
        ),
        # Live trials grouped by status -- backs the queue stats
        # aggregation in ``oddish.queue.get_queue_stats``.
        Index(
            "idx_trials_live_org_status",
            "org_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Probe-runs listing (``list_org_probes_core``): seek by org, then
        # walk per-task newest-first for the window aggregation without a
        # separate sort. Partial on ``is_probe`` since probe trials are a
        # small slice of the table.
        Index(
            "idx_trials_probe_org_task_created",
            "org_id",
            "task_id",
            text("created_at DESC"),
            postgresql_where=text("is_probe"),
        ),
    )


class AnalysisCostModel(TimestampedMixin, Base):
    """Append-only ledger of analysis-job LLM spend.

    Distinct from ``trials.cost_usd`` (the solving agent's run). One row per
    analysis-job execution. ``trial_id`` is a plain indexed string (no DB FK)
    and is nullable because experiment-level jobs have no single trial.
    """

    __tablename__ = "analysis_costs"
    __table_args__ = (
        Index("ix_analysis_costs_job_kind", "job_kind"),
        Index("ix_analysis_costs_trial_id", "trial_id"),
        Index("ix_analysis_costs_experiment_id", "experiment_id"),
        Index("ix_analysis_costs_org_id", "org_id"),
        Index("ix_analysis_costs_task_id", "task_id"),
        Index("ix_analysis_costs_analyzer_id", "analyzer_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    job_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billed_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # AnalyzerModel/report association. Task-level QA and classifiers leave
    # this NULL and reconcile through task_id/trial_id instead.
    analyzer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "native" = harness-reported (CLI total_cost_usd); "estimated" = priced
    # via model_pricing. Job A is always "native".
    cost_source: Mapped[str] = mapped_column(String(16), nullable=False)


class ModalCostSpanModel(TimestampedMixin, Base):
    """Append-only ledger of per-trial Modal compute spend, one row per
    billable container span.

    Compute sibling of ``analysis_costs`` (LLM spend): a span is one billable
    container — the Modal worker function babysitting a job
    (``worker_function``), the harbor agent sandbox (``agent_sandbox``), or a
    separate verifier sandbox (``verifier_sandbox``). Scope columns are plain
    indexed strings with no FKs (clone of ``analysis_costs``). ``finished_at``
    is NULL while the span is open; every close is a CAS update
    (``UPDATE ... SET finished_at = ... WHERE id = ... AND finished_at IS
    NULL``) so late hooks, settlement, and the reconciliation sweep cannot
    double-close. Pricing lives in :mod:`oddish.costs.modal_cost`.
    """

    __tablename__ = "modal_costs"
    __table_args__ = (
        # One row per (job attempt, role, open-ordinal). ``span_ordinal`` is a
        # per-key open counter so harbor-internal retries and failed starts
        # each get their own row. Rows with NULL ``worker_job_id`` (sandbox
        # spans recorded outside a known job context) never conflict.
        UniqueConstraint(
            "worker_job_id",
            "worker_job_attempt",
            "component_role",
            "span_ordinal",
            name="uq_modal_costs_job_attempt_role_ordinal",
        ),
        # One row per provider-visible container id. Partial: spans recorded
        # before the provider id is known carry NULL ``external_id``.
        Index(
            "uq_modal_costs_provider_external_id",
            "provider",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_modal_costs_span_order",
        ),
        CheckConstraint(
            "cpu_request IS NULL OR cpu_request >= 0",
            name="ck_modal_costs_cpu_request_nonneg",
        ),
        CheckConstraint(
            "cpu_limit IS NULL OR cpu_limit >= 0",
            name="ck_modal_costs_cpu_limit_nonneg",
        ),
        CheckConstraint(
            "mem_request_mb IS NULL OR mem_request_mb >= 0",
            name="ck_modal_costs_mem_request_nonneg",
        ),
        CheckConstraint(
            "mem_limit_mb IS NULL OR mem_limit_mb >= 0",
            name="ck_modal_costs_mem_limit_nonneg",
        ),
        CheckConstraint(
            "gpu_count IS NULL OR gpu_count >= 0",
            name="ck_modal_costs_gpu_count_nonneg",
        ),
        Index("ix_modal_costs_trial_id", "trial_id"),
        Index("ix_modal_costs_experiment_id", "experiment_id"),
        Index("ix_modal_costs_org_id", "org_id"),
        # Dashboard bucket/window axis: the settled view groups on
        # ``finished_at`` (matching inference), not ``created_at``.
        Index("ix_modal_costs_finished_at", "finished_at"),
        # Open spans awaiting close — the reconciliation sweep scans these and
        # joins ``worker_jobs`` on ``worker_job_id`` to find terminal jobs.
        Index(
            "ix_modal_costs_open_spans",
            "worker_job_id",
            postgresql_where=text("finished_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    trial_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billed_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Normally set; NULL only for sandbox spans recorded outside a known job
    # context. Text because worker-side job ids are unbounded today.
    worker_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_job_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``trials.attempts`` at span open — informational only (worker spans for
    # non-trial jobs have no trial attempt at all).
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "worker_function" | "agent_sandbox" | "verifier_sandbox"
    component_role: Mapped[str] = mapped_column(String(32), nullable=False)
    span_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How the timer boundaries were observed:
    # "hooks" | "phase_timing" | "reaped" | "reconciled" | "backfill"
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    # "pinned" | "override" | "provider_default" | "unknown"
    spec_source: Mapped[str] = mapped_column(String(16), nullable=False)
    cpu_request: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_request_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_enforcement_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mem_enforcement_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Modal billing name (post-normalization), e.g. "H100", "A100-80GB".
    gpu_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nonpreemptible surcharge: 3 for the worker function, 1 for sandboxes.
    # Applies only to the cpu+mem terms, never GPU.
    price_multiplier: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # usd_per_sec values actually used, as strings (Decimal-exact).
    rate_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # sku -> modal_rates.id of the chosen rate rows (None for code fallback).
    rate_ids: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    # Set when cost_usd is NULL: "unknown_gpu" | "no_resources" | "no_rate".
    unpriced_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="estimated"
    )
    estimator_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ModalRateModel(TimestampedMixin, Base):
    """Append-only compute rate card: usd-per-second by provider and sku.

    A price change appends a new row with a later ``effective_at``; pricing
    picks the newest row with ``effective_at <= span.started_at`` so old
    estimates stay reproducible. Seeded by migration ``modal_costs_001`` from
    modal.com/pricing; :data:`oddish.costs.modal_cost.DEFAULT_RATES` is the
    code-constant fallback mirroring those seed rows.
    """

    __tablename__ = "modal_rates"
    __table_args__ = (
        # Also the lookup index for (provider, sku, effective_at) selection.
        UniqueConstraint(
            "provider",
            "sku",
            "effective_at",
            name="uq_modal_rates_provider_sku_effective",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # "function:cpu_core_sec" | "function:mem_gib_sec" |
    # "sandbox:cpu_core_sec" | "sandbox:mem_gib_sec" | "gpu:<TYPE>"
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    usd_per_sec: Mapped[Decimal] = mapped_column(Numeric(16, 10), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Provenance, e.g. "modal.com/pricing 2026-07-22".
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class TrialEventModel(Base):
    """Live transcript event for a running trial (short-lived; S3 is the record)."""

    __tablename__ = "trial_events"

    trial_id: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("trials.id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
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
    # When the current lease was taken. Lets the reconciler reclaim a leaked
    # lease per-slot (keyed on the owning worker's liveness) while still
    # honoring a short grace window for the brief acquire->claim gap.
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "idx_queue_slots_queue_key_locked_until",
            "queue_key",
            "locked_until",
        ),
    )


class ProviderCapacityLeaseModel(Base):
    """Weighted provider-wide lease, including FIFO capacity waiters."""

    __tablename__ = "provider_capacity_leases"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    requested_memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'WAITING'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "requested_memory_mb > 0", name="ck_provider_capacity_memory_positive"
        ),
        CheckConstraint(
            "state IN ('WAITING', 'HELD')", name="ck_provider_capacity_state"
        ),
        Index(
            "ix_provider_capacity_leases_admission",
            "provider",
            "state",
            "created_at",
            "owner_id",
        ),
        Index("ix_provider_capacity_leases_expiry", "lease_expires_at"),
    )


class WorkerJobModel(TimestampedMixin, Base):
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

    # Harbor execution variant routing id ('default' | '<registry-id>' |
    # 'ephemeral'). Part of the effective dispatch key: the dispatcher
    # discovers/counts/spawns per (queue_key, harbor_variant_id) and the claim
    # is scoped to it.
    harbor_variant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'default'")
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
            "harbor_variant_id",
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
        # Recent-terminal branch of ``fetch_visible_worker_jobs``: we
        # filter by ``finished_at IS NOT NULL`` and ORDER BY
        # ``finished_at DESC``, so the partial index gives us a tight
        # range scan keyed on the subject pair.
        Index(
            "idx_worker_jobs_subject_finished_recent",
            "subject_table",
            "subject_id",
            "finished_at",
            postgresql_where=text("finished_at IS NOT NULL"),
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
        Index(
            "uq_worker_jobs_tag_project_active",
            "kind",
            "subject_table",
            "subject_id",
            unique=True,
            postgresql_where=text(
                "kind = 'TAG_PROJECT' "
                "AND status IN ('QUEUED', 'RETRYING') "
                "AND subject_id IS NOT NULL"
            ),
        ),
    )
    provider: Mapped[str] = mapped_column(Text, nullable=True)
    external_id: Mapped[str] = mapped_column(Text, nullable=True)

    # Per-stage timing for the pre-harbor preamble (design spec §12). The
    # existing claimed_at/started_at cover claim+total-elapsed; these fill the
    # submit -> spawn -> sandbox-create gap so the 180s-poll vs cold-start vs
    # image-pull split is visible.
    spawned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sandbox_creating_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Why a still-waiting job has not been spawned -- the queryable why-waiting /
    # admission-reason field (spec §12): "waiting for slot", "cold-starting",
    # "capability-rejected: <table>", ...
    admission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Job-scoped credential token lifecycle (spec §6.6). Only the SHA-256 hash is
    # persisted; the raw token + scoped bundle (model keys, S3 prefix) are
    # returned to the worker at claim and held in memory. Revoked on terminal
    # status. Gated by settings.job_scoped_tokens_enabled (default off).
    job_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_token_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class APIKeyModel(TimestampedMixin, Base):
    """API key for programmatic access.

    API keys are scoped to an organization and have specific permissions.
    The actual key is only shown once on creation; we store a hash.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)

    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

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

    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Status and expiry
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Visibility
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        Index("idx_api_keys_org_id", "org_id"),
        Index("idx_api_keys_key_hash", "key_hash"),
    )


# ---------------------------------------------------------------------------
# Soft-delete registration
# ---------------------------------------------------------------------------
#
# Register the oddish-owned mapped classes so the session-level filter in
# ``soft_delete.py`` auto-applies ``deleted_at IS NULL`` to their reads.
# ``TaskVersionModel`` and ``WorkerJobModel`` inherit ``deleted_at`` via
# ``TimestampedMixin`` but are intentionally *not* soft-deletable:
#
# * ``TaskVersionModel`` rows are immutable history of a task; deletion
#   is exclusively via the parent task's CASCADE.
# * ``WorkerJobModel`` rows model scheduling state; the queue uses
#   ``status = 'CANCELLED'`` to retire jobs (see ``delete_*_core``),
#   not ``deleted_at``.
#
# Backend-only auth models (organizations / users) register
# themselves from ``backend/models.py`` so this module stays standalone.
# ``APIKeyModel`` lives in this module, but its soft-delete registration
# still happens from ``backend/models.py`` (alongside the other auth models).
class TagModel(TimestampedMixin, Base):
    """Org-scoped custom tag definition (the 'vocabulary' row)."""

    __tablename__ = "tags"
    __table_args__ = (
        Index(
            "uq_tags_org_normalized",
            text("COALESCE(org_id, '')"),
            "normalized_key",
            text("COALESCE(normalized_value, '')"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND state <> 'DELETED'"),
        ),
        Index("idx_tags_org_state", "org_id", "state"),
        Index("idx_tags_org_visibility", "org_id", "visibility"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    visibility: Mapped[TagVisibility] = mapped_column(
        SQLEnum(
            TagVisibility,
            name="tag_visibility",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagVisibility.PRIVATE,
        server_default=TagVisibility.PRIVATE.value,
    )
    state: Mapped[TagState] = mapped_column(
        SQLEnum(
            TagState,
            name="tag_state",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagState.ACTIVE,
        server_default=TagState.ACTIVE.value,
    )
    merged_into_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tags.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TagAssignmentModel(TimestampedMixin, Base):
    """A tag attached to a target (version/task/experiment)."""

    __tablename__ = "tag_assignments"
    __table_args__ = (
        Index(
            "uq_tag_assignments_target",
            text("COALESCE(org_id, '')"),
            "tag_id",
            "scope",
            "target_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_tag_assignments_tag_scope_state", "tag_id", "scope", "state"),
        Index("idx_tag_assignments_scope_target_state", "scope", "target_id", "state"),
        Index("idx_tag_assignments_org_tag_state", "org_id", "tag_id", "state"),
        Index(
            "idx_tag_assignments_source_experiment",
            "source_experiment_id",
            postgresql_where=text("source_experiment_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    tag_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scope: Mapped[TagAssignmentScope] = mapped_column(
        SQLEnum(
            TagAssignmentScope,
            name="tag_assignment_scope",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[TagAssignmentState] = mapped_column(
        SQLEnum(
            TagAssignmentState,
            name="tag_assignment_state",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagAssignmentState.ACTIVE,
        server_default=TagAssignmentState.ACTIVE.value,
    )
    source: Mapped[TagAssignmentSource] = mapped_column(
        SQLEnum(
            TagAssignmentSource,
            name="tag_assignment_source",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagAssignmentSource.DIRECT,
        server_default=TagAssignmentSource.DIRECT.value,
    )
    source_experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_assignment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    assigned_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("now()"),
        nullable=False,
    )
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TagExclusionModel(TimestampedMixin, Base):
    """Per-experiment opt-out for a living tag inheritance."""

    __tablename__ = "tag_exclusions"
    __table_args__ = (
        Index(
            "uq_tag_exclusions_target",
            "experiment_id",
            "tag_id",
            "scope",
            "target_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_tag_exclusions_tag_id", "tag_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    tag_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[TagAssignmentScope] = mapped_column(
        SQLEnum(
            TagAssignmentScope,
            name="tag_assignment_scope",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TagGrantModel(TimestampedMixin, Base):
    """Per-tag delegation of a definition-plane capability."""

    __tablename__ = "tag_grants"
    __table_args__ = (
        Index(
            "uq_tag_grants_principal",
            "tag_id",
            "principal_type",
            text("COALESCE(principal_user_id, '')"),
            "capability",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_tag_grants_tag_id", "tag_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    tag_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    principal_type: Mapped[TagGrantPrincipal] = mapped_column(
        SQLEnum(
            TagGrantPrincipal,
            name="tag_grant_principal",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    principal_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capability: Mapped[TagGrantCapability] = mapped_column(
        SQLEnum(
            TagGrantCapability,
            name="tag_grant_capability",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    granted_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TagEventModel(Base):
    """Append-only audit row for every tag state change. Not soft-deleted."""

    __tablename__ = "tag_events"
    __table_args__ = (
        Index("idx_tag_events_org_tag_occurred_at", "org_id", "tag_id", "occurred_at"),
        Index("idx_tag_events_event_uuid", "event_uuid", unique=True),
    )

    id: Mapped[int] = mapped_column(
        Integer().with_variant(BigInteger(), "postgresql"),
        primary_key=True,
        autoincrement=True,
    )
    event_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[TagEventAction] = mapped_column(
        SQLEnum(
            TagEventAction,
            name="tag_event_action",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    tag_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[TagAssignmentScope | None] = mapped_column(
        SQLEnum(
            TagAssignmentScope,
            name="tag_assignment_scope",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=True,
    )
    target_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_type: Mapped[TagEventActor] = mapped_column(
        SQLEnum(
            TagEventActor,
            name="tag_event_actor",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagEventActor.USER,
        server_default=TagEventActor.USER.value,
    )
    source: Mapped[TagEventSource] = mapped_column(
        SQLEnum(
            TagEventSource,
            name="tag_event_source",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagEventSource.API,
        server_default=TagEventSource.API.value,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("now()"),
        nullable=False,
    )


class TagPolicyModel(Base):
    """Per-org governance configuration for tags."""

    __tablename__ = "tag_policies"

    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_tags_per_entity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    name_max_len: Mapped[int] = mapped_column(
        Integer, nullable=False, default=64, server_default="64"
    )
    name_charset: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="[a-z0-9._-]",
        server_default="[a-z0-9._-]",
    )
    reserved_prefixes: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    who_can_create: Mapped[TagPolicyWhoCanCreate] = mapped_column(
        SQLEnum(
            TagPolicyWhoCanCreate,
            name="tag_policy_who_can_create",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagPolicyWhoCanCreate.ANY_MEMBER,
        server_default=TagPolicyWhoCanCreate.ANY_MEMBER.value,
    )
    profanity_mode: Mapped[TagPolicyProfanityMode] = mapped_column(
        SQLEnum(
            TagPolicyProfanityMode,
            name="tag_policy_profanity_mode",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=TagPolicyProfanityMode.ENFORCE,
        server_default=TagPolicyProfanityMode.ENFORCE.value,
    )
    profanity_allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    profanity_denylist: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
        nullable=False,
    )


class SavedTagFilterModel(TimestampedMixin, Base):
    """A named tag-filter (saved AND/OR/NOT AST) per user or org."""

    __tablename__ = "saved_tag_filters"
    __table_args__ = (
        Index(
            "uq_saved_tag_filters_owner_name",
            "org_id",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_saved_tag_filters_org_visibility", "org_id", "visibility"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    filter_ast: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    visibility: Mapped[SavedTagFilterVisibility] = mapped_column(
        SQLEnum(
            SavedTagFilterVisibility,
            name="saved_tag_filter_visibility",
            native_enum=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=SavedTagFilterVisibility.PRIVATE,
        server_default=SavedTagFilterVisibility.PRIVATE.value,
    )


class SkillModel(TimestampedMixin, Base):
    """A Claude Code skill (a SKILL.md directory) shared within an org.

    Seed skills (``is_seed=True``, ``org_id`` NULL) are global read-only
    built-ins. Custom skills are org-scoped and record the uploading user
    in ``created_by_user_id``. The skill's files live in ``SkillFileModel``
    rows keyed by ``relative_path`` so the directory tree is reconstructable
    without object storage.
    """

    __tablename__ = "skills"
    __table_args__ = (
        # Reuse a skill name after soft-delete: partial unique index matching
        # the ``deleted_at IS NULL`` predicate the soft-delete listener appends.
        # COALESCE handles NULL org_id (OSS/global) the same way presets do.
        Index(
            "idx_skills_unique_org_name",
            text("COALESCE(org_id, '')"),
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    operator_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_focus: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_metric: Mapped[str | None] = mapped_column(String(32), nullable=True)

    files: Mapped[list["SkillFileModel"]] = relationship(  # type: ignore[assignment]
        "SkillFileModel",
        back_populates="skill",
        cascade="all, delete-orphan",
        order_by="SkillFileModel.relative_path",
        lazy="selectin",
    )


class SkillFileModel(Base):
    """One file inside a skill, e.g. ``SKILL.md`` or ``scripts/run.sh``.

    Not soft-deletable on its own — files are owned by their skill and
    cascade with it. ``relative_path`` encodes the directory shape.
    """

    __tablename__ = "skill_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    skill_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    skill: Mapped["SkillModel"] = relationship(  # type: ignore[assignment]
        "SkillModel", back_populates="files"
    )


class DocumentModel(TimestampedMixin, Base):
    """An uploaded reference document for agent retrieval.

    Raw bytes live in S3 (``s3_key_raw``); the agent-facing ``digest_text``
    and ``summary`` are Claude-generated at ingest. Org-scoped and records
    the uploading user. ``content_text`` is the extracted raw text, retained
    for a future semantic-search upgrade but not searched in v1.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_org_id", "org_id"),
        Index("ix_documents_created_by_user_id", "created_by_user_id"),
        Index("ix_documents_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # source_type is one of: upload | paste | link
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    digest_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    s3_key_raw: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    raw_mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)


class QueueRuntimeStatusModel(Base):
    """Per-component runtime heartbeat the admin dashboard reads back.

    Written via raw SQL in ``workers/queue/runtime_status.py`` and created by
    migration. Modeled here so ``create_all`` materializes it on preview DBs:
    previews ``stamp`` migrations instead of running them, so a raw-SQL-only
    table would otherwise be absent (see ``backend/preview_seed.py`` and
    ``.github/scripts/preview/bootstrap_preview_db.py``).
    """

    __tablename__ = "queue_runtime_status"

    component: Mapped[str] = mapped_column(Text, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class TagProjectionSweepStateModel(Base):
    """Singleton (1-row) checkpoint for the hourly tag-projection reconciler.

    Holds ``last_full_sweep_at``; read/written via raw SQL in
    ``workers/queue/cleanup.py`` and created by migration ``aa04ta05sweep``.
    Modeled here for the same reason as :class:`QueueRuntimeStatusModel` — so
    ``create_all`` builds it on preview DBs.
    """

    __tablename__ = "tag_projection_sweep_state"
    __table_args__ = (CheckConstraint("id", name="tag_sweep_singleton"),)

    id: Mapped[bool] = mapped_column(
        Boolean, primary_key=True, server_default=text("true")
    )
    last_full_sweep_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PromptKind(str, Enum):
    """The slot a prompt fills. Exactly one ``prompts`` row exists per kind;
    stored as a plain string column so the vocabulary can grow without a
    Postgres enum migration. Enforced at the API boundary, not the DB."""

    QA_PRE_TRIAL = "QA_PRE_TRIAL"
    QA_POST_TRIAL = "QA_POST_TRIAL"
    TRAJECTORY_SUMMARY = "TRAJECTORY_SUMMARY"


class QAStage(str, Enum):
    """The QA lifecycle point an assignment fires at. Values match the
    corresponding ``AnalyzerType`` so a stage and the block it runs read the
    same in logs. Stored as a plain string column, like ``PromptKind``."""

    PRE_TRIAL = "pre_trial"
    POST_TRIAL = "post_trial"


class CostExcludedLlmKeyModel(TimestampedMixin, Base):
    """An LLM provider API key whose spend is excluded from cost accounting.

    The admin-managed list of sponsored/free keys. Only the one-way ``key_hash``
    (SHA-256) is stored -- exclusion is pure equality matching against
    ``trials.llm_key_hash``, never key reuse -- plus a masked ``key_hint`` for
    display; the plaintext key is never persisted. ``deleted_at`` (soft delete)
    is the live/removed state, and the partial UNIQUE keeps one live row per hash
    so a removed key can be re-added.
    """

    __tablename__ = "cost_excluded_llm_keys"
    __table_args__ = (
        Index(
            "idx_cost_excluded_llm_keys_hash_live",
            "key_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hint: Mapped[str] = mapped_column(String(8), nullable=False, server_default="")
    label: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PromptModel(TimestampedMixin, Base):
    """A versioned analyzer prompt, one row per kind and optional scope. The highest
    ``prompt_versions.version`` is always the one that runs; editing appends
    a new version (no activation pointer)."""

    __tablename__ = "prompts"
    __table_args__ = (
        Index(
            "idx_prompts_unique_kind_scope",
            "kind",
            text("COALESCE(org_id, '')"),
            text("COALESCE(scope_type, '')"),
            text("COALESCE(scope_id, '')"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_prompts_org_id", "org_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # NULL/NULL is the installation-wide default. Hosted callers may create
    # org, user, experiment, task, or trial overrides. IDs intentionally have
    # no hosted-auth FKs so the core package remains self-hostable.
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    versions: Mapped[list["PromptVersionModel"]] = relationship(  # type: ignore[assignment]
        "PromptVersionModel",
        back_populates="prompt",
        cascade="all, delete-orphan",
        order_by="PromptVersionModel.version",
        lazy="selectin",
    )


class PromptVersionModel(Base):
    """One immutable revision of a prompt's content."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint(
            "prompt_id", "version", name="uq_prompt_versions_prompt_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    prompt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prompt: Mapped["PromptModel"] = relationship(  # type: ignore[assignment]
        "PromptModel", back_populates="versions"
    )


class QAAssignmentModel(TimestampedMixin, Base):
    """A reusable prompt job attached to a QA lifecycle scope."""

    __tablename__ = "qa_assignments"
    __table_args__ = (
        Index("ix_qa_assignments_org_scope", "org_id", "scope_type", "scope_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # A direct FK to one prompt row -- including that row's own scope -- rather
    # than a kind string, so execution never re-runs kind resolution.
    prompt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False
    )
    # NULL inherits the registry's latest-wins; set to pin one version.
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    # Unlike ``prompts``, both columns are NOT NULL, so installation-wide rows
    # are spelled ("global", "") rather than (NULL, NULL). See GLOBAL_SCOPE_ID
    # in oddish.core.qa_assignments -- writes normalize through it.
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_client_type: Mapped[str] = mapped_column(String(32), nullable=False)
    allow_oddish_cli: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # false at a narrow scope suppresses a broader row for the same
    # (stage, prompt kind) instead of adding a job.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


from oddish.db.soft_delete import register_soft_delete_models

register_soft_delete_models(
    ExperimentModel,
    AnalyzerModel,
    AnalyzerBlockModel,
    PromptModel,
    AnalyzerRunModel,
    TaskModel,
    TrialModel,
    TagModel,
    TagAssignmentModel,
    TagExclusionModel,
    TagGrantModel,
    SavedTagFilterModel,
    SkillModel,
    DocumentModel,
    CostExcludedLlmKeyModel,
    PromptModel,
    QAAssignmentModel,
)
