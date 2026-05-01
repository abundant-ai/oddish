"""Task-first Oddish: target shape."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AgentSpec(BaseModel):
    agent: str
    model: str | None = None
    harbor_config_hash: str
    equivalence_key: str  # sha256(agent | model | harbor_config_hash)


class Task(BaseModel):
    id: str
    name: str
    current_version_id: str | None
    org_id: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime
    tags: dict[str, str] = Field(default_factory=dict)


class TaskVersion(BaseModel):
    """Immutable. New version on every re-upload."""

    id: str
    task_id: str
    version: int
    content_hash: str
    task_s3_key: str | None = None
    message: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime


class TrialOrigin(str, Enum):
    ODDISH = "oddish"
    IMPORTED = "imported"


class TrialStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Trial(BaseModel):
    """Atom of evidence. Pinned to (TaskVersion, AgentSpec).
    No experiment_id -- experiments are read-side views over trials."""

    id: str
    task_version_id: str
    agent_spec: AgentSpec
    job_id: str | None  # null for IMPORTED
    origin: TrialOrigin = TrialOrigin.ODDISH
    status: TrialStatus
    reward: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class ValidationStatus(str, Enum):
    UNVALIDATED = "unvalidated"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class ValidationOutcome(BaseModel):
    """Derived verdict on a TaskVersion, recomputable from trials."""

    task_version_id: str
    status: ValidationStatus
    n_trials_considered: int
    pass_rate: float | None = None
    notes: str | None = None
    decided_at: datetime | None = None
    decided_by_user_id: str | None = None


class JobKind(str, Enum):
    VALIDATION = "validation"
    EXPERIMENT_BACKFILL = "experiment_backfill"
    AD_HOC = "ad_hoc"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCell(BaseModel):
    task_version_id: str
    agent_spec: AgentSpec
    n_trials: int = Field(ge=1)


class Job(BaseModel):
    """Ad-hoc execution batch. Replaces 'experiment as runner'."""

    id: str
    kind: JobKind
    status: JobStatus
    cells: list[JobCell]
    triggered_by_experiment_id: str | None = None
    launched_by_user_id: str | None = None
    launched_at: datetime
    finished_at: datetime | None = None


class ExperimentCell(BaseModel):
    task_version_id: str
    agent_spec: AgentSpec
    target_n_trials: int = Field(ge=1)


class Experiment(BaseModel):
    """Saved spec + view. Owns no trials."""

    id: str
    name: str
    cells: list[ExperimentCell]
    description: str | None = None
    is_public: bool = False
    public_token: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime


class ExperimentCellStatus(BaseModel):
    task_version_id: str
    agent_equivalence_key: str
    target_n_trials: int
    have_n_successful: int
    have_n_total: int
    gap: int


class ExperimentView(BaseModel):
    experiment: Experiment
    cells: list[ExperimentCellStatus]
