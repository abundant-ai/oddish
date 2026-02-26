from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from harbor.models.agent.name import AgentName
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import MCPServerConfig as MCPServerSpec

from oddish.db import AnalysisStatus, Priority, TaskStatus, TrialStatus, VerdictStatus

_MODEL_ABSENT_ALIASES: set[str] = {"", "-", "none", "null", "nil", "n/a", "na", "default"}


class ArtifactSpec(BaseModel):
    """Specification for a file to extract from the sandbox after execution."""

    path: str
    destination: str | None = None


# =============================================================================
# Request Schemas
# =============================================================================


class TrialSpec(BaseModel):
    """Specification for a single trial (API input).

    Not Harbor's AgentConfig — this is an API-facing schema with simpler field
    names (``agent`` vs ``name``, ``model`` vs ``model_name``,
    ``timeout_minutes`` vs ``override_timeout_sec``).  Translation to Harbor's
    AgentConfig happens in oddish's queue/runner layer.
    """

    agent: str = Field(
        ..., description="Agent name (e.g., 'claude-code', 'codex', 'gemini-cli')"
    )
    model: str | None = Field(
        None, description="Model name (e.g., 'claude-sonnet-4-20250514')"
    )
    timeout_minutes: int = Field(60, description="Trial timeout in minutes")
    environment: EnvironmentType | None = Field(
        None, description="Execution backend override"
    )
    agent_env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables injected into the agent sandbox",
    )
    agent_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific keyword arguments forwarded to Harbor's AgentConfig.kwargs (e.g. max_thinking_tokens)",
    )

    @model_validator(mode="after")
    def normalize_model_aliases(self) -> "TrialSpec":
        if self.model is None:
            return self
        normalized = self.model.strip()
        if normalized.lower() in _MODEL_ABSENT_ALIASES:
            self.model = None
            return self
        self.model = normalized
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


class HarborPassthroughConfig(BaseModel):
    """Harbor configuration fields passed through to the execution layer.

    Shared by TaskSubmission and TaskSweepSubmission to avoid duplication.
    """

    # Verifier
    disable_verification: bool = Field(
        False,
        description="Skip test verification (useful for RL rollout generation)",
    )
    verifier_timeout_sec: float | None = Field(
        None,
        description="Override the default verifier timeout in seconds",
    )

    # Environment resources
    env_cpus: int | None = Field(None, description="Override sandbox CPU count")
    env_memory_mb: int | None = Field(None, description="Override sandbox memory in MB")
    env_storage_mb: int | None = Field(
        None, description="Override sandbox storage in MB"
    )
    env_gpus: int | None = Field(None, description="Override sandbox GPU count")
    env_gpu_types: list[str] | None = Field(
        None,
        description="Acceptable GPU types (e.g. ['A100', 'H100']). None means any type.",
    )
    allow_internet: bool | None = Field(
        None,
        description="Whether to allow internet access in sandbox (None = use task default)",
    )
    agent_setup_timeout_sec: float | None = Field(
        None,
        description="Override agent setup/installation timeout in seconds",
    )
    docker_image: str | None = Field(
        None,
        description="Prebuilt Docker image to use instead of building from task Dockerfile",
    )
    mcp_servers: list[MCPServerSpec] | None = Field(
        None,
        description="MCP servers to make available in the task environment",
    )
    artifacts: list[ArtifactSpec | str] | None = Field(
        None,
        description="Files to extract from sandbox after execution",
    )

    # Modal sandbox lifecycle
    sandbox_timeout_secs: int | None = Field(
        None,
        description="Maximum lifetime of a Modal sandbox in seconds (default 86400 = 24h)",
    )
    sandbox_idle_timeout_secs: int | None = Field(
        None,
        description="Seconds of inactivity before a Modal sandbox is terminated",
    )

    # Daytona sandbox lifecycle
    auto_stop_interval_mins: int | None = Field(
        None,
        description="Minutes of inactivity before a Daytona sandbox is stopped (0 = no auto-stop)",
    )
    auto_delete_interval_mins: int | None = Field(
        None,
        description="Minutes after stop before a Daytona sandbox is deleted (0 = immediate)",
    )
    snapshot_template_name: str | None = Field(
        None,
        description="Daytona snapshot template name for faster env init (use {name} placeholder)",
    )


class TaskSubmission(HarborPassthroughConfig):
    """Task submission request (API input)."""

    task_path: str = Field(..., description="Path to Harbor task directory")
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_path if not provided)",
    )
    trials: list[TrialSpec] = Field(..., description="List of trials to run")
    user: str = Field(..., description="Submitting user")
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
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

    @model_validator(mode="after")
    def require_models(self):
        allowed_missing = {AgentName.NOP.value, AgentName.ORACLE.value}
        for trial in self.trials:
            if trial.agent not in allowed_missing and not trial.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self


class TaskSweepSubmission(HarborPassthroughConfig):
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
            "user": "alice"
        }
    """

    task_id: str = Field(..., description="Task ID from /tasks/upload")
    name: str | None = Field(
        None,
        description="Human-readable task name (derived from task_id if not provided)",
    )

    configs: list[AgentModelPair] = Field(
        ..., description="List of agent/model pairs with individual trial counts"
    )

    # Common fields
    user: str = Field(..., description="Submitting user")
    priority: Priority = Field(Priority.LOW, description="Priority: 'high' or 'low'")
    experiment_id: str | None = Field(None, description="Optional experiment ID")
    tags: dict[str, str] = Field(default_factory=dict, description="Optional tags")
    timeout_minutes: int = Field(60, description="Default trial timeout in minutes")
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

    @model_validator(mode="after")
    def require_models(self):
        allowed_missing = {AgentName.NOP.value, AgentName.ORACLE.value}
        for config in self.configs:
            if config.agent not in allowed_missing and not config.model:
                raise ValueError("Model is required for all agents except nop/oracle")
        return self


class ExperimentUpdateRequest(BaseModel):
    """Request to update experiment metadata."""

    name: str = Field(..., description="Experiment name")


# =============================================================================
# Response Schemas
# =============================================================================


class UploadResponse(BaseModel):
    """Task upload response."""

    task_id: str
    name: str
    task_path: str | None = None
    s3_key: str | None = None


class TrialResponse(BaseModel):
    id: str
    name: str
    task_id: str
    task_path: str
    agent: str
    provider: str
    queue_key: str
    model: str | None
    status: TrialStatus = Field(
        ...,
        description="Execution status: 'success'=completed (regardless of test result), 'failed'=execution error",
    )
    attempts: int
    max_attempts: int
    harbor_stage: str | None
    reward: int | None = Field(
        None,
        description="Test result: 1=passed, 0=failed, null=no result (separate from execution status)",
    )
    error_message: str | None
    result: dict | None

    # Token usage & cost
    input_tokens: int | None = Field(
        None, description="Total input tokens (including cache hits)"
    )
    cache_tokens: int | None = Field(None, description="Cache tokens used")
    output_tokens: int | None = Field(None, description="Output tokens generated")
    cost_usd: float | None = Field(None, description="Estimated cost in USD")

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
    created_at: datetime


class ExperimentUpdateResponse(BaseModel):
    id: str
    name: str


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
    total: int
    completed: int
    failed: int
    progress: str  # e.g., "5/10 completed"
    reward_success: int | None = None
    reward_total: int | None = None
    run_analysis: bool = False
    verdict_status: VerdictStatus | None = None
    verdict: dict | None = None
    verdict_error: str | None = Field(
        None,
        description="Error message if verdict computation failed",
    )
    trials: list[TrialResponse] | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}
