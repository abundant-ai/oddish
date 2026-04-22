"""Handler registry for the unified ``worker_jobs`` dispatcher.

Each ``WorkerJobKind`` has at most one handler at a time. The dispatcher
(see ``oddish.workers.queue.worker_job_single_job.run_single_worker_job``)
routes every claimed row through the handler registered for its kind.

``JobOutcome`` is the only shape a handler is allowed to return: either
``success`` (with an optional small ``result_summary`` blob) or
``failure`` (with an error message and a retryable flag). The
``exactly-one-set`` invariant is enforced in ``__post_init__`` so a
buggy handler can't stall a row in ``RUNNING``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from oddish.db import WorkerJobKind


class HandlerAlreadyRegisteredError(RuntimeError):
    """Raised when two distinct handlers try to claim the same kind."""


class NoHandlerRegisteredError(LookupError):
    """Raised when the dispatcher looks up a handler that was never registered."""


@dataclass
class JobSuccess:
    """Terminal-success shape for a ``worker_jobs`` row."""

    result_summary: dict[str, Any] | None = None


@dataclass
class JobFailure:
    """Terminal-failure shape; retryable=False marks it permanent."""

    error_message: str
    retryable: bool = True


@dataclass
class JobOutcome:
    """The only thing a ``JobHandler.run`` is allowed to return.

    Construct with ``JobOutcome.ok(...)`` / ``JobOutcome.fail(...)`` in
    handler code; the raw dataclass is still exposed so tests can
    enforce shape invariants directly.
    """

    success: JobSuccess | None = None
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        if (self.success is None) == (self.failure is None):
            raise ValueError(
                "JobOutcome requires exactly one of success / failure to be set"
            )

    @classmethod
    def ok(cls, result_summary: dict[str, Any] | None = None) -> "JobOutcome":
        return cls(success=JobSuccess(result_summary=result_summary))

    @classmethod
    def fail(cls, error_message: str, *, retryable: bool = True) -> "JobOutcome":
        return cls(failure=JobFailure(error_message=error_message, retryable=retryable))


@runtime_checkable
class JobHandler(Protocol):
    """Structural protocol every handler follows.

    The registry uses ``isinstance`` checks against this protocol only
    in tests; production code just relies on the three attribute
    lookups below. Keep the surface minimal.
    """

    kind: WorkerJobKind

    def default_queue_key(self, job: Any) -> str: ...
    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def run(self, job: Any) -> JobOutcome: ...


HANDLERS: dict[WorkerJobKind, JobHandler] = {}


def register(handler: JobHandler) -> JobHandler:
    """Install ``handler`` in the global registry.

    Double-registering the same instance is a no-op so decorator-style
    usage at module-load plus an explicit ``ensure_builtin_...`` call
    doesn't raise. Registering a *different* handler for a kind that's
    already taken is an error -- two handlers silently racing would be
    worse than the crash.
    """
    kind = handler.kind
    existing = HANDLERS.get(kind)
    if existing is handler:
        return handler
    if existing is not None:
        raise HandlerAlreadyRegisteredError(
            f"Handler for kind={kind.value!r} already registered: {existing!r}"
        )
    HANDLERS[kind] = handler
    return handler


def get_handler(kind: WorkerJobKind) -> JobHandler:
    try:
        return HANDLERS[kind]
    except KeyError as exc:
        raise NoHandlerRegisteredError(
            f"No handler registered for kind={kind.value!r}"
        ) from exc


def clear_handlers() -> None:
    """Drop every registered handler (test-only entry point)."""
    HANDLERS.clear()


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
]
