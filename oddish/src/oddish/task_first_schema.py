from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel


class Task(BaseModel):
    id: str
    name: str


class TaskVersion(BaseModel):
    id: str
    task_id: str
    version: int
    content_hash: str
    bundle_s3_key: str
    created_at: datetime


class Agent(BaseModel):
    harness: str
    model: str
    provider: str


class Trial(BaseModel):
    """Pure evidence row. Written when execution finishes."""

    id: str
    task_version_id: str
    agent: Agent
    job_id: str | None  # user-visible batch
    worker_job_id: str | None  # null for IMPORTED trials
    reward: float | None
    error_message: str | None
    created_at: datetime


class WorkerJobKind(str, Enum):
    TRIAL = "trial"
    ANALYSIS = "analysis"


class WorkerJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerJob(BaseModel):
    """A unit of queued work. Polymorphic by kind."""

    id: str
    kind: WorkerJobKind
    queue_key: str  # slot pool (usually per-provider)
    status: WorkerJobStatus
    attempts: int
    worker_id: str | None
    claimed_at: datetime | None
    heartbeat_at: datetime | None
    job_id: str | None  # user-visible batch this work belongs to
    payload: dict[str, Any]  # kind-specific input
    created_at: datetime
    finished_at: datetime | None


class QueueSlot(BaseModel):
    queue_key: str
    slot: int
    locked_by: str | None
    locked_until: datetime | None


class Job(BaseModel):
    id: str
    cells: list["JobCell"]
    launched_at: datetime


class JobCell(BaseModel):
    task_version_id: str
    agent: Agent
    n_trials: int


class Experiment(BaseModel):
    id: str
    name: str
    cells: list["ExperimentCell"]


class ExperimentCell(BaseModel):
    task_version_id: str
    agent: Agent
    target_n_trials: int


class ResolvedExperimentCell(BaseModel):
    cell: ExperimentCell
    trials: list[Trial]


class ResolvedExperiment(BaseModel):
    experiment: Experiment
    cells: list[ResolvedExperimentCell]
