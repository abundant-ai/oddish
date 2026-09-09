"""Handler registry for the unified ``worker_jobs`` dispatcher.

Each ``WorkerJobKind`` has at most one handler at a time. The dispatcher
(see ``oddish.workers.queue.worker_job_single_job.run_single_worker_job``)
routes every claimed row through the handler registered for its kind.

``JobOutcome`` is the only shape a handler is allowed to return: ``success``
(with an optional small ``result_summary`` blob), ``failure`` (with an error
message and a retryable flag), or ``reroute`` (a request for a durable provider
handoff). The exactly-one-set invariant is enforced in ``__post_init__``.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    retry_after_seconds: float | None = None


@dataclass
class JobReroute:
    """Non-failure request to move a job onto another execution lane."""

    target_environment: str
    target_execution_lane: str
    reason: str
    retry_after_seconds: float | None = None
    subject_attempt: int | None = None


@dataclass
class JobOutcome:
    """The only thing a ``JobHandler.run`` is allowed to return.

    Construct with ``JobOutcome.ok(...)``, ``JobOutcome.fail(...)``, or
    ``JobOutcome.reroute_to(...)`` in handler code. Persisting a reroute is a
    separate dispatcher concern so the transition can update the domain row,
    job row, sandbox ledger, and capacity lease atomically.
    """

    success: JobSuccess | None = None
    failure: JobFailure | None = None
    reroute: JobReroute | None = None

    def __post_init__(self) -> None:
        if sum(
            value is not None for value in (self.success, self.failure, self.reroute)
        ) != 1:
            raise ValueError(
                "JobOutcome requires exactly one of success / failure / reroute "
                "to be set"
            )

    @classmethod
    def ok(cls, result_summary: dict[str, Any] | None = None) -> "JobOutcome":
        return cls(success=JobSuccess(result_summary=result_summary))

    @classmethod
    def fail(
        cls,
        error_message: str,
        *,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
    ) -> "JobOutcome":
        return cls(
            failure=JobFailure(
                error_message=error_message,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
            )
        )

    @classmethod
    def reroute_to(
        cls,
        *,
        target_environment: str,
        target_execution_lane: str,
        reason: str,
        retry_after_seconds: float | None = None,
        subject_attempt: int | None = None,
    ) -> "JobOutcome":
        return cls(
            reroute=JobReroute(
                target_environment=target_environment,
                target_execution_lane=target_execution_lane,
                reason=reason,
                retry_after_seconds=retry_after_seconds,
                subject_attempt=subject_attempt,
            )
        )


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


def register(handler: JobHandler, *, override: bool = False) -> JobHandler:
    """Install ``handler`` in the global registry.

    Double-registering the same instance is a no-op so decorator-style
    usage at module-load plus an explicit ``ensure_builtin_...`` call
    doesn't raise. Registering a *different* handler for a kind that's
    already taken is an error -- two handlers silently racing would be
    worse than the crash -- unless ``override`` is set, which the hosted
    backend uses to swap in a cloud-only handler at container load.
    """
    kind = handler.kind
    existing = HANDLERS.get(kind)
    if existing is handler:
        return handler
    if existing is not None and not override:
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
    "JobReroute",
    "JobSuccess",
    "NoHandlerRegisteredError",
    "clear_handlers",
    "get_handler",
    "register",
]
