from __future__ import annotations

__version__ = "0.2.0"

_EXPORTS: dict[str, tuple[str, str]] = {
    # Config
    "settings": ("oddish.config", "settings"),
    # DB - Enums
    "TaskStatus": ("oddish.db", "TaskStatus"),
    "JobStatus": ("oddish.db", "JobStatus"),
    "TrialStatus": ("oddish.db", "TrialStatus"),
    "Priority": ("oddish.db", "Priority"),
    # DB - Models
    "TaskModel": ("oddish.db", "TaskModel"),
    "TrialModel": ("oddish.db", "TrialModel"),
    # DB - Connection
    "init_db": ("oddish.db", "init_db"),
    "get_session": ("oddish.db", "get_session"),
    "get_pool": ("oddish.db", "get_pool"),
    "install_pgqueuer": ("oddish.db", "install_pgqueuer"),
    # Schemas - Request
    "TaskSubmission": ("oddish.schemas", "TaskSubmission"),
    "TaskSweepSubmission": ("oddish.schemas", "TaskSweepSubmission"),
    "TrialSpec": ("oddish.schemas", "TrialSpec"),
    "AgentModelPair": ("oddish.schemas", "AgentModelPair"),
    # Schemas - Response
    "TaskResponse": ("oddish.schemas", "TaskResponse"),
    "TaskStatusResponse": ("oddish.schemas", "TaskStatusResponse"),
    "TrialResponse": ("oddish.schemas", "TrialResponse"),
    # Queue
    "create_task": ("oddish.queue", "create_task"),
    "get_task_with_trials": ("oddish.queue", "get_task_with_trials"),
    "get_queue_stats": ("oddish.queue", "get_queue_stats"),
    "get_pipeline_stats": ("oddish.queue", "get_pipeline_stats"),
    "enqueue_trial": ("oddish.queue", "enqueue_trial"),
    # Harbor
    "run_harbor_trial": ("oddish.workers", "run_harbor_trial"),
    "HarborOutcome": ("oddish.workers", "HarborOutcome"),
    # Workers
    "run_queue_worker": ("oddish.workers", "run_queue_worker"),
}

__all__ = ["__version__", *_EXPORTS.keys()]


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module 'oddish' has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
