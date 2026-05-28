from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from harbor.models.agent.name import AgentName
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import RetryConfig as HarborRetryConfig
from harbor.models.task.config import MCPServerConfig as MCPServerSpec
from harbor.models.trial.config import (
    AgentConfig as HarborAgentConfig,
    ArtifactConfig as HarborArtifactConfig,
    EnvironmentConfig as HarborEnvironmentConfig,
    VerifierConfig as HarborVerifierConfig,
)

from oddish.config import normalize_model_id
from oddish.db import (
    AnalysisStatus,
    Priority,
    TaskStatus,
    TrialOrigin,
    TrialStatus,
    VerdictStatus,
)


# =============================================================================
# Harbor Execution Config (wraps Harbor's native types)
# =============================================================================


class HarborConfig(BaseModel):
    """Structured Harbor execution config using Harbor's native types.

    Embeds Harbor's EnvironmentConfig, VerifierConfig, and ArtifactConfig
    directly so that new Harbor fields are automatically available without
    Oddish-side changes.
    """

    environment: HarborEnvironmentConfig = Field(
        default_factory=HarborEnvironmentConfig
    )
    verifier: HarborVerifierConfig = Field(default_factory=HarborVerifierConfig)
    artifacts: list[str | HarborArtifactConfig] = Field(default_factory=list)

    timeout_multiplier: float | None = Field(
        None,
        description=(
            "Global multiplier applied to all Harbor timeouts. Overrides the "
            "JobConfig default of 1.0 when set."
        ),
    )
    agent_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the agent execution timeout only.",
    )
    verifier_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the verifier timeout only.",
    )
    agent_setup_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the agent setup timeout only.",
    )
    environment_build_timeout_multiplier: float | None = Field(
        None,
        description="Multiplier for the environment build timeout only.",
    )
    retry: HarborRetryConfig | None = Field(
        None,
        description=(
            "Harbor RetryConfig for trial-level retries (max_retries, wait "
            "multipliers, include/exclude exceptions). Uses Harbor's default "
            "when omitted."
        ),
    )

    docker_image: str | None = Field(
        None,
        description="Prebuilt Docker image (patched into task.toml, not a JobConfig field)",
    )
    mcp_servers: list[MCPServerSpec] | None = Field(
        None,
        description="MCP servers to make available in the task environment",
    )


# =============================================================================
# Request Schemas
# =============================================================================


class TrialSpec(BaseModel):
    """Specification for a single trial (API input).

    ``agent`` and ``model`` identify *what* to run.  Per-trial Harbor overrides
    (env vars, kwargs, timeouts) live in the optional ``agent_config``.
    """

    agent: str = Field(
        ..., description="Agent name (e.g., 'claude-code', 'codex', 'gemini-cli')"
    )
    model: str | None = Field(
        None, description="Model name (e.g., 'claude-sonnet-4-20250514')"
    )
    timeout_minutes: int | None = Field(
        None,
        description="Deprecated. Oddish now requires timeouts to be declared in task.toml.",
    )
    environment: EnvironmentType | None = Field(
        None, description="Execution backend override"
    )
    agent_config: HarborAgentConfig | None = Field(
        None,
        description="Per-trial Harbor AgentConfig overrides (env vars, kwargs, setup timeout, etc.)",
    )

    @model_validator(mode="after")
    def normalize_model_aliases(self) -> "TrialSpec":
        self.model = normalize_model_id(self.model)
        return self

    @model_validator(mode="after")
    def reject_timeout_override(self) -> "TrialSpec":
        if (
            "timeout_minutes" in self.model_fields_set
            and self.timeout_minutes is not None
        ):
            raise ValueError(
                "timeout_minutes is no longer supported. "
                "Set explicit [agent].timeout_sec, [verifier].timeout_sec "
                "(or timeout_sec on every [[verifiers]] stage), and "
                "[environment].build_timeout_sec in task.toml."
            )
        return self


class AgentModelPair(TrialSpec):
    """Specification for agent/model combination with trial count.

    Extends TrialSpec with sweep-specific fields (n_trials, concurrency).
    """

    n_trials: int = Field(
        1, ge=1, description="Number of trials for this agent/model pair"
    )
    concurrency: int | None = Field(
        None,
        ge=1,
        description="(Deprecated) Max parallel trials for this agent",
    )


class TaskSubmission(BaseModel):
    """Task submission request (API input)."""

    task_path: str = Field(..., description="Path to Harbor task directory")
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_path if not provided)",
    )
    trials: list[TrialSpec] = Field(..., description="List of trials to run")
    user: str | None = Field(
        None,
        description="Submitting user (resolved server-side from auth when omitted)",
    )
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
    max_trial_attempts: int = Field(
        6,
        ge=1,
        description=(
            "Maximum Oddish worker attempts per trial, including the initial "
            "attempt. For example, 3 allows the initial run plus up to 2 retries."
        ),
    )
    experiment_id: str | None = Field(None, description="Optional experiment ID")
    tags: dict[str, str] = Field(default_factory=dict, description="Optional tags")
    run_analysis: bool = Field(
        False,
        description="If True, run LLM analysis on each trial after completion and compute task verdict",
    )
    github_username: str | None = Field(
        None,
        description="GitHub username to attribute this task to (recorded as metadata)",
    )
    harbor: HarborConfig = Field(
        default_factory=HarborConfig,  # type: ignore[arg-type]
        description="Harbor execution config (environment, verifier, artifacts, etc.)",
    )
    content_hash: str | None = Field(
        None,
        description="Deterministic hash of task directory contents (set by CLI during upload)",
    )

    @model_validator(mode="after")
    def require_models(self):
        allowed_missing = {AgentName.NOP.value, AgentName.ORACLE.value}
        for trial in self.trials:
            if trial.agent not in allowed_missing and not trial.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self


class TaskSweepSubmission(BaseModel):
    """Convenience API for the common workflow: one task + many agent/model pairs.

    The server expands this into a normal TaskSubmission with trials for each agent/model pair.

    Examples:
        # Multiple agent/model pairs with different trial counts
        {
            "task_id": "abc123",
            "configs": [
                {"agent": "claude-code", "model": "claude-sonnet-4-5", "n_trials": 3},
                {"agent": "terminus-2", "model": "gemini-3-pro-preview", "n_trials": 5},
            ],
            "user": "alice",
            "harbor": {"verifier": {"disable": true}}
        }
    """

    task_id: str = Field(
        ...,
        description=(
            "Task ID from /tasks/upload/init and /tasks/upload/complete, or an "
            "existing task ID when append_to_task is true"
        ),
    )
    append_to_task: bool = Field(
        False,
        description=(
            "If true, append new trials to an existing task instead of creating "
            "a new task row"
        ),
    )
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_id if not provided)",
    )

    configs: list[AgentModelPair] = Field(
        ..., description="List of agent/model pairs with individual trial counts"
    )

    # Common fields
    user: str | None = Field(
        None,
        description="Submitting user (resolved server-side from auth when omitted)",
    )
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
    max_trial_attempts: int = Field(
        6,
        ge=1,
        description=(
            "Maximum Oddish worker attempts per trial, including the initial "
            "attempt. Applies to all trials created by this sweep submission."
        ),
    )
    experiment_id: str | None = Field(None, description="Optional experiment ID")
    tags: dict[str, str] = Field(default_factory=dict, description="Optional tags")
    timeout_minutes: int | None = Field(
        None,
        description="Deprecated. Oddish now requires timeouts to be declared in task.toml.",
    )
    environment: EnvironmentType | None = Field(
        None, description="Default execution backend override"
    )
    run_analysis: bool = Field(
        False,
        description="If True, run LLM analysis on each trial after completion and compute task verdict",
    )
    github_username: str | None = Field(
        None,
        description="GitHub username to attribute this task to (recorded as metadata)",
    )
    publish_experiment: bool | None = Field(
        None,
        description="If true, publish the experiment for public read-only access",
    )
    harbor: HarborConfig = Field(
        default_factory=HarborConfig,  # type: ignore[arg-type]
        description="Harbor execution config (environment, verifier, artifacts, etc.)",
    )
    content_hash: str | None = Field(
        None,
        description="Deterministic hash of task directory contents (set by CLI during upload)",
    )

    @model_validator(mode="after")
    def require_models(self):
        allowed_missing = {AgentName.NOP.value, AgentName.ORACLE.value}
        for config in self.configs:
            if config.agent not in allowed_missing and not config.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self

    @model_validator(mode="after")
    def reject_timeout_override(self) -> "TaskSweepSubmission":
        if (
            "timeout_minutes" in self.model_fields_set
            and self.timeout_minutes is not None
        ):
            raise ValueError(
                "timeout_minutes is no longer supported. "
                "Set explicit [agent].timeout_sec, [verifier].timeout_sec "
                "(or timeout_sec on every [[verifiers]] stage), and "
                "[environment].build_timeout_sec in task.toml."
            )
        return self


class ExperimentUpdateRequest(BaseModel):
    """Request to update experiment metadata."""

    name: str = Field(..., description="Experiment name")


class ExperimentCombineRequest(BaseModel):
    """Request to combine several experiments into one result experiment.

    The named source experiments are left untouched; a brand-new result
    experiment is created and the underlying data (task memberships and
    finished trials, plus their artifacts) of every source is copied into
    it.
    """

    source_experiment_ids: list[str] = Field(
        ...,
        description=(
            "IDs (or names) of the experiments to combine. At least two "
            "distinct sources are required."
        ),
    )
    name: str | None = Field(
        None,
        description=(
            "Name for the result experiment. A human-friendly name is "
            "generated when omitted."
        ),
    )
    copy_artifacts: bool = Field(
        True,
        description=(
            "When True (default) each copied trial gets its own duplicate of "
            "the source trial's S3 artifacts so the result experiment is fully "
            "independent. When False the copied trials reference the source "
            "trials' artifacts in place (cheaper, but shared storage)."
        ),
    )

    @model_validator(mode="after")
    def _validate_sources(self) -> "ExperimentCombineRequest":
        # Preserve order while dropping blanks/duplicates so the same
        # experiment can't be combined with itself into a doubled result.
        deduped = list(
            dict.fromkeys(
                stripped
                for s in self.source_experiment_ids
                if s and (stripped := s.strip())
            )
        )
        if len(deduped) < 2:
            raise ValueError(
                "source_experiment_ids must contain at least two distinct experiments"
            )
        self.source_experiment_ids = deduped
        if self.name is not None:
            self.name = self.name.strip() or None
        return self


# =============================================================================
# Response Schemas
# =============================================================================


class TaskUploadInitRequest(BaseModel):
    """Request to prepare a task upload."""

    name: str = Field(..., description="Task name derived from the local directory")
    content_hash: str = Field(
        ..., description="Deterministic hash of the task directory contents"
    )
    message: str | None = Field(
        None, description="Optional description of what changed in this version"
    )
    force_new_version: bool = Field(
        False,
        description=(
            "Allocate a new task version even when the content hash matches the "
            "latest existing version. Used when callers need a fresh version "
            "stamp (e.g. to flip run_analysis on)."
        ),
    )


class TaskUploadCompleteRequest(BaseModel):
    """Request to finalize a direct-to-storage task upload."""

    task_id: str
    name: str
    version: int = Field(..., ge=1)
    content_hash: str = Field(
        ..., description="Deterministic hash of the uploaded task directory contents"
    )
    message: str | None = Field(
        None, description="Optional description of what changed in this version"
    )
    register_task: bool = Field(
        False,
        description=(
            "If True, persist a TaskModel + v1 TaskVersionModel row when the "
            "task does not yet exist. Use this for upload-only flows "
            "(`oddish upload`) so the task becomes visible in the UI even "
            "without any trials. The sweep path leaves this False and "
            "continues to create the task row itself."
        ),
    )
    user: str | None = Field(
        None,
        description=(
            "Submitting user name used when `register_task=True` creates a "
            "new TaskModel. Ignored when the task already exists."
        ),
    )
    priority: Priority | None = Field(
        None,
        description=(
            "Priority used when `register_task=True` creates a new TaskModel. "
            "Defaults to LOW. Ignored when the task already exists."
        ),
    )


class UploadResponse(BaseModel):
    """Task upload response."""

    task_id: str
    name: str
    task_path: str | None = None
    s3_key: str | None = None
    version: int | None = None
    version_id: str | None = None
    existing_task: bool = False
    content_unchanged: bool = False
    content_hash: str | None = None


class TaskUploadInitResponse(UploadResponse):
    """Task upload preparation response."""

    upload_url: str | None = None
    upload_method: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    requires_completion: bool = False


class TrialQueueInfo(BaseModel):
    position: int | None = Field(
        None,
        description="1-based live queue position for queued/retrying trials in the current scheduler snapshot",
    )
    ahead: int | None = Field(
        None,
        description="Number of queued/retrying trials currently ahead of this trial",
    )
    queued_count: int = Field(
        ...,
        description="Total queued/retrying trials currently in this queue",
    )
    running_count: int = Field(
        ...,
        description="Total running trials currently in this queue",
    )
    concurrency_limit: int = Field(
        ...,
        description="Configured concurrency limit for this queue key",
    )


class TaskVersionResponse(BaseModel):
    """Response for a single task version."""

    id: str
    task_id: str
    version: int
    task_path: str
    task_s3_key: str | None = None
    content_hash: str | None = None
    message: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskVersionSummary(BaseModel):
    """Per-version aggregates used by the task detail view."""

    id: str
    version: int
    message: str | None = None
    created_at: datetime
    is_current: bool = False
    trial_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    pass_count: int = 0
    partial_count: int = 0
    fail_count: int = 0
    pending_count: int = 0
    reward_sum: float = 0.0
    reward_total: int = 0
    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    last_run_at: datetime | None = None


class TaskCostTotals(BaseModel):
    """Task-wide cost rollup across every (non-superseded) trial."""

    cost_usd: float = 0.0
    cost_trial_count: int = 0
    cost_has_estimated: bool = False
    cost_has_native: bool = False
    total_trials: int = 0


class TaskDetailResponse(BaseModel):
    """Task detail bundle for ``GET /tasks/{task_id}/detail``."""

    task: "TaskStatusResponse"
    versions: list[TaskVersionSummary] = Field(default_factory=list)
    totals: TaskCostTotals = Field(default_factory=TaskCostTotals)


class VisibleWorkerJob(BaseModel):
    id: str
    kind: str
    status: str
    queue_key: str
    subject_table: str | None = None
    subject_id: str | None = None
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class TrialResponse(BaseModel):
    id: str
    name: str
    task_id: str
    task_path: str
    task_version: int | None = None
    task_version_id: str | None = None
    experiment_id: str | None = None
    agent: str
    provider: str
    queue_key: str
    model: str | None
    status: TrialStatus = Field(
        ...,
        description="Execution status: 'success'=completed (regardless of test result), 'failed'=execution error",
    )
    origin: TrialOrigin = Field(
        TrialOrigin.ODDISH,
        description=(
            "Where this trial was executed. 'oddish' = ran on Oddish's "
            "worker runtime (default). 'imported' = uploaded from an "
            "external Harbor run via `oddish import`."
        ),
    )
    attempts: int
    max_attempts: int
    harbor_stage: str | None
    reward: float | None = Field(
        None,
        description=(
            "Verifier score in [0, 1]: 1=full pass, 0=full fail, "
            "partial values indicate partial credit; null=no result"
        ),
    )
    error_message: str | None
    result: dict | None

    # Token usage & cost
    input_tokens: int | None = Field(
        None, description="Total input tokens (including cache hits)"
    )
    cache_tokens: int | None = Field(None, description="Cache tokens used")
    output_tokens: int | None = Field(None, description="Output tokens generated")
    cost_usd: float | None = Field(
        None,
        description=(
            "Trial cost in USD. Native value from the agent runtime when "
            "available; otherwise estimated from token counts and a static "
            "model pricing table (see ``cost_is_estimated``)."
        ),
    )
    cost_is_estimated: bool | None = Field(
        None,
        description=(
            "True when ``cost_usd`` was derived from the static model "
            "pricing table because the agent runtime did not report a "
            "native cost. False when the cost came directly from the "
            "runtime. Null when no cost is available."
        ),
    )

    # Per-phase timing breakdown
    phase_timing: dict | None = Field(
        None,
        description="Per-phase duration breakdown: {environment_setup, agent_setup, agent_execution, verifier}",
    )

    # Trajectory
    has_trajectory: bool = Field(
        False, description="Whether an ATIF trajectory file exists for this trial"
    )

    analysis_status: AnalysisStatus | None = None
    analysis: dict | None = Field(
        None,
        description="Trial analysis with classification (GOOD_SUCCESS, BAD_FAILURE, etc.), subtype, and recommendation",
    )
    analysis_error: str | None = Field(
        None,
        description="Error message if analysis failed",
    )
    superseded_by_trial_id: str | None = Field(
        None,
        description=(
            "Set when this trial has been replaced by a user-driven "
            "retry that spawned a brand-new immutable trial. Default "
            "list/aggregate endpoints filter superseded rows out; this "
            "field lets the UI navigate the rerun chain when surfacing "
            "history."
        ),
    )
    jobs: list[VisibleWorkerJob] = Field(
        default_factory=list,
        description="Active/recent worker_jobs rows for this trial",
    )
    queue_info: TrialQueueInfo | None = Field(
        None,
        description="Live queue snapshot for queued/retrying trials",
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class TaskResponse(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    trials_count: int
    providers: dict[str, int]  # provider -> count of trials
    experiment_id: str | None = None
    experiment_name: str | None = None
    created_at: datetime
    new_trial_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of the trials created by this sweep submission. "
            "For append-mode submissions, this contains only the newly appended "
            "trials (not any pre-existing trials on the task). Clients can use "
            "this to filter status/watch views to only the trials they just "
            "submitted."
        ),
    )


class TaskBatchCancelRequest(BaseModel):
    task_ids: list[str] = Field(
        default_factory=list,
        description="Task IDs to cancel in one request",
    )


class ExperimentUpdateResponse(BaseModel):
    id: str
    name: str


class ExperimentCombineResponse(BaseModel):
    """Result of combining several experiments."""

    id: str = Field(..., description="ID of the newly created result experiment")
    name: str = Field(..., description="Name of the result experiment")
    source_experiment_ids: list[str] = Field(
        ..., description="Resolved IDs of the experiments that were combined"
    )
    tasks_linked: int = Field(
        0, description="Distinct tasks linked into the result experiment"
    )
    trials_copied: int = Field(
        0, description="Finished trials copied into the result experiment"
    )
    trials_skipped: int = Field(
        0,
        description=(
            "Source trials skipped because they were not finished "
            "(still pending/queued/running) at combine time"
        ),
    )
    artifacts_copied: int = Field(
        0, description="S3 objects duplicated for the copied trials"
    )


class TaskBrowseExperiment(BaseModel):
    id: str
    name: str


class TaskBrowseTrial(BaseModel):
    id: str
    name: str
    status: TrialStatus
    reward: float | None = None
    error_message: str | None = None


class TaskBrowseItem(BaseModel):
    id: str
    name: str
    current_version: int | None = None
    current_version_id: str | None = None
    version_count: int
    total_trials: int
    completed_trials: int
    failed_trials: int
    reward_success: int
    reward_sum: float
    reward_total: int
    last_run_at: datetime | None = None
    latest_trials: list[TaskBrowseTrial] = Field(default_factory=list)
    experiments: list[TaskBrowseExperiment] = Field(default_factory=list)


class TaskBrowseResponse(BaseModel):
    items: list[TaskBrowseItem]
    limit: int
    offset: int
    has_more: bool


class TaskStatusResponse(BaseModel):
    id: str
    name: str
    status: TaskStatus
    priority: Priority
    user: str
    github_username: str | None = None
    github_meta: dict[str, str] | None = None
    task_path: str
    experiment_id: str
    experiment_name: str
    experiment_is_public: bool = False
    experiment_created_at: datetime | None = None
    current_version: int | None = None
    current_version_id: str | None = None
    total: int
    completed: int
    failed: int
    progress: str  # e.g., "5/10 completed"
    reward_success: int | None = None
    reward_sum: float | None = None
    reward_total: int | None = None
    run_analysis: bool = False
    verdict_status: VerdictStatus | None = None
    verdict: dict | None = None
    verdict_error: str | None = Field(
        None,
        description="Error message if verdict computation failed",
    )
    jobs: list[VisibleWorkerJob] = Field(
        default_factory=list,
        description="Active/recent worker_jobs rows for this task and its trials",
    )
    trials: list[TrialResponse] | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


# =============================================================================
# Trial Import (off-oddish Harbor runs -> existing task)
# =============================================================================


class ImportedTrialSpec(BaseModel):
    """Per-trial metadata for an off-oddish Harbor execution.

    The CLI extracts these fields from a ``harbor.models.trial.result.TrialResult``
    and posts them to ``/trials/import/init``. The server creates a
    ``TrialModel`` row in terminal state with ``origin=IMPORTED`` and
    returns a presigned PUT URL for the artifact tarball; the client
    then PUTs the archive and calls ``/trials/import/complete``.
    """

    agent: str = Field(..., description="Agent name (e.g., 'claude-code')")
    model: str | None = Field(
        None, description="Model name (normalized server-side via settings)"
    )
    environment: EnvironmentType | None = Field(
        None, description="Execution backend that actually ran the trial"
    )
    status: TrialStatus = Field(
        TrialStatus.SUCCESS,
        description=(
            "Terminal status for the imported trial. Must be SUCCESS or "
            "FAILED -- imports never enter the queue."
        ),
    )
    reward: float | None = Field(
        None, description="Verifier score in [0, 1]; None if no verifier result"
    )
    error_message: str | None = Field(
        None, description="Execution error message, if any"
    )
    harbor_stage: str | None = Field(
        "completed",
        description="Harbor lifecycle stage (defaults to 'completed' for imports)",
    )
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    phase_timing: dict | None = Field(
        None,
        description=(
            "Per-phase duration breakdown matching the live schema: "
            "{environment_setup, agent_setup, agent_execution, verifier}"
        ),
    )
    has_trajectory: bool = Field(
        False, description="Whether the uploaded archive contains a trajectory file"
    )
    harbor_config: dict | None = Field(
        None,
        description="Serialized Harbor config used during external execution",
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    external_trial_id: str | None = Field(
        None,
        description=(
            "Harbor TrialResult UUID (or any stable external ID). Stored as "
            "the trial's idempotency_key; re-imports with the same key are "
            "rejected by the unique index."
        ),
    )

    @model_validator(mode="after")
    def _validate_terminal_status(self) -> "ImportedTrialSpec":
        if self.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
            raise ValueError("Imported trials must have status SUCCESS or FAILED")
        return self

    @model_validator(mode="after")
    def _normalize_model(self) -> "ImportedTrialSpec":
        self.model = normalize_model_id(self.model)
        return self


class TrialImportInitRequest(BaseModel):
    """Request to create an imported trial row + presigned artifact URL."""

    task_id: str = Field(
        ..., description="Existing task ID (upload via `oddish upload` first)"
    )
    experiment_id: str | None = Field(
        None,
        description=(
            "Experiment ID or name to attach the trial to. Creates the "
            "experiment if the name does not exist. When None, a fresh "
            "auto-named experiment is created (matching `oddish run`'s "
            "default behaviour)."
        ),
    )
    trial: ImportedTrialSpec = Field(..., description="Imported trial metadata")
    upload_artifacts: bool = Field(
        True,
        description=(
            "When True, the response includes a presigned PUT URL for a "
            "``.oddish-trial-import.tar.gz`` staging archive that the "
            "client then uploads and finalizes with /trials/import/complete. "
            "When False, the trial row is created without any artifacts "
            "and complete does not need to be called."
        ),
    )


class TrialImportInitResponse(BaseModel):
    """Response for `/trials/import/init`."""

    trial_id: str
    task_id: str
    experiment_id: str
    experiment_name: str
    trial_s3_key: str | None = Field(
        None,
        description="S3 prefix where the trial artifacts will live once uploaded",
    )
    archive_s3_key: str | None = Field(
        None,
        description="S3 key the client should PUT the archive tarball to",
    )
    upload_url: str | None = Field(
        None, description="Presigned PUT URL for the archive"
    )
    upload_method: str | None = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    requires_completion: bool = Field(
        False,
        description="Whether the client must call /trials/import/complete after PUT",
    )


class TrialImportCompleteRequest(BaseModel):
    """Finalize an imported trial after the artifact archive was uploaded."""

    trial_id: str


class TrialImportCompleteResponse(BaseModel):
    """Response for `/trials/import/complete`."""

    trial_id: str
    trial_s3_key: str
    files_extracted: int


# =============================================================================
# Public Sharing Models
# =============================================================================


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
