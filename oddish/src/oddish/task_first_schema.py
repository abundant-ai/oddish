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
    created_at: datetime


class Agent(BaseModel):
    id: str
    name: str
    model: str | None
    config_hash: str


class Trial(BaseModel):
    id: str
    task_version_id: str
    agent_id: str
    job_id: str | None
    reward: float | None
    created_at: datetime


class Job(BaseModel):
    id: str
    cells: list["JobCell"]
    launched_at: datetime


class JobCell(BaseModel):
    task_version_id: str
    agent_id: str
    n_trials: int


class Experiment(BaseModel):
    id: str
    name: str
    cells: list["ExperimentCell"]


class ExperimentCell(BaseModel):
    task_version_id: str
    agent_id: str
    target_n_trials: int
