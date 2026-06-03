"""Worker-job package: handler registry, enqueue surface, and built-in handlers.

The unified ``worker_jobs`` table decouples scheduling state from domain
state. This package is the seam: it owns

- ``registry``: the ``JobHandler`` protocol, global ``HANDLERS`` dict,
  ``register`` / ``get_handler`` / ``clear_handlers`` helpers, and the
  ``JobOutcome`` result type that handlers return.
- ``enqueue``: ``EnqueueRequest`` plus ``enqueue_worker_job`` which
  writes a ``worker_jobs`` row inside the caller's session.
- ``handlers``: per-kind adapters (``TrialJobHandler`` /
  ``AnalysisJobHandler`` / ``VerdictJobHandler`` / ``TaskExpandJobHandler``)
  that delegate to the existing ``run_*_job`` bodies and map terminal
  domain state back onto a ``JobOutcome``.

``ensure_builtin_handlers_registered()`` wires every built-in handler
into the global registry. Both the standalone worker and the backend
call it at container load.
"""

from oddish.db import WorkerJobKind
from oddish.workers.jobs.registry import (
    HANDLERS,
    HandlerAlreadyRegisteredError,
    JobFailure,
    JobHandler,
    JobOutcome,
    JobSuccess,
    NoHandlerRegisteredError,
    clear_handlers,
    get_handler,
    register,
)
from oddish.workers.jobs.enqueue import EnqueueRequest, enqueue_worker_job


_BUILTINS_REGISTERED = False


def ensure_builtin_handlers_registered() -> None:
    """Register every built-in ``JobHandler`` exactly once.

    Idempotent: callers can invoke it at module load without worrying
    about cross-module double-registration. The first call wins; later
    calls no-op.
    """
    global _BUILTINS_REGISTERED
    required_kinds = {
        WorkerJobKind.TRIAL,
        WorkerJobKind.ANALYSIS,
        WorkerJobKind.VERDICT,
        WorkerJobKind.TASK_EXPAND,
    }
    if _BUILTINS_REGISTERED and required_kinds.issubset(HANDLERS):
        return

    # Lazy imports keep ``oddish.workers.queue`` off the critical
    # import path for code that only needs ``enqueue_worker_job``.
    from oddish.workers.jobs.handlers import (
        AnalysisJobHandler,
        TaskExpandJobHandler,
        TrialJobHandler,
        VerdictJobHandler,
    )

    for handler in (
        TrialJobHandler(),
        AnalysisJobHandler(),
        VerdictJobHandler(),
        TaskExpandJobHandler(),
    ):
        try:
            register(handler)
        except HandlerAlreadyRegisteredError:
            # Tolerate re-registration when a test re-imports the module.
            continue

    _BUILTINS_REGISTERED = True


__all__ = [
    "HANDLERS",
    "HandlerAlreadyRegisteredError",
    "JobFailure",
    "JobHandler",
    "JobOutcome",
    "JobSuccess",
    "NoHandlerRegisteredError",
    "clear_handlers",
    "get_handler",
    "register",
    "EnqueueRequest",
    "enqueue_worker_job",
    "ensure_builtin_handlers_registered",
]
