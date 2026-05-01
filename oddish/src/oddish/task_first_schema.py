from datetime import datetime
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
    id: str
    task_version_id: str
    agent: Agent
    job_id: str | None
    reward: float | None
    created_at: datetime


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
