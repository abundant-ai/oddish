"""Single-job runner over the unified `worker_jobs` table.

The only dispatcher path after the cutover from the legacy
per-kind claim SQLs. Kind-agnostic: claims one row with
``FOR UPDATE SKIP LOCKED`` and hands it to the registered
``JobHandler`` for the row's ``kind``.

All scheduling-state transitions (``QUEUED`` / ``RETRYING`` →
``RUNNING`` → ``SUCCESS`` / ``RETRYING`` / ``FAILED``) happen here.
Handlers still do their own domain writes (``trials.status``,
``tasks.verdict`` ...) inside ``JobHandler.run``; the runner only
touches ``worker_jobs``.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from oddish.db import WorkerJobKind, WorkerJobStatus
from oddish.workers.jobs.registry import (
    JobOutcome,
    NoHandlerRegisteredError,
    get_handler,
)
from oddish.workers.queue.db_proxy import get_db_proxy
from oddish.workers.queue.shared import console


# Callback invoked after a claimed row completes successfully. Kept as a
# simple ``kind -> async fn(subject_id)`` dict so the backend can wire
# GitHub notifications (trial / analysis / verdict) without pushing
# backend-specific concerns into this module.
PostSuccessHooks = dict[WorkerJobKind, Callable[[str], Awaitable[None]]]

TRIAL_RETRY_BASE_DELAY_SECONDS = 30.0
TRIAL_RATE_LIMIT_RETRY_BASE_DELAY_SECONDS = 300.0
TRIAL_RETRY_MAX_DELAY_SECONDS = 1800.0
TRIAL_RETRY_JITTER_FRACTION = 0.25

_RATE_LIMIT_RE = re.compile(
    r"\b("
    r"429|"
    r"too many requests|"
    r"rate[\s_-]*limit(?:ed|s|ing)?|"
    r"ratelimit(?:ed|s|ing)?|"
    r"quota(?: exceeded)?|"
    r"resource[_\s-]*exhausted|"
    r"requests per minute|"
    r"tokens per minute|"
    r"throttl(?:ed|ing)?"
    r")\b",
    re.IGNORECASE,
)


def _ensure_handlers_registered() -> None:
    """Register built-in handlers lazily on first claim.

    The runner imports from ``oddish.workers.jobs.registry`` at module
    load (for JobOutcome / get_handler), but we defer pulling in the
    handler implementations until first use because ``handlers.py``
    imports back into this file for ``ClaimedWorkerJob``. Calling this
    at run time (by which point every module has finished initializing)
    breaks the cycle cleanly.
    """
    from oddish.workers.jobs import ensure_builtin_handlers_registered

    ensure_builtin_handlers_registered()


__all__ = [
    "ClaimedWorkerJob",
    "claim_single_worker_job",
    "run_single_worker_job",
]


def classify_retry_reason(error_message: str | None) -> str:
    """Return a coarse retry reason for scheduling/telemetry."""
    if error_message and _RATE_LIMIT_RE.search(error_message):
        return "rate_limit"
    return "transient"


def calculate_trial_retry_delay_seconds(
    *,
    attempts: int,
    error_message: str | None,
    jitter: float | None = None,
) -> float:
    """Return bounded exponential trial retry delay with multiplicative jitter.

    ``attempts`` is the attempt that just failed. A first failed attempt gets
    the base delay, the second gets 2x, and so on. Rate-limit-looking errors
    start at a higher base because immediately retrying usually makes the
    provider-side contention worse.
    """
    retry_reason = classify_retry_reason(error_message)
    base_delay = (
        TRIAL_RATE_LIMIT_RETRY_BASE_DELAY_SECONDS
        if retry_reason == "rate_limit"
        else TRIAL_RETRY_BASE_DELAY_SECONDS
    )
    exponential_delay = base_delay * (2 ** max(attempts - 1, 0))
    capped_delay = min(exponential_delay, TRIAL_RETRY_MAX_DELAY_SECONDS)
    jitter_value = (
        random.uniform(0.0, TRIAL_RETRY_JITTER_FRACTION) if jitter is None else jitter
    )
    jitter_value = max(0.0, min(jitter_value, TRIAL_RETRY_JITTER_FRACTION))
    return min(
        capped_delay * (1.0 + jitter_value),
        TRIAL_RETRY_MAX_DELAY_SECONDS,
    )


@dataclass(frozen=True)
class ClaimedWorkerJob:
    id: str
    kind: WorkerJobKind
    queue_key: str
    subject_table: str | None
    subject_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    org_id: str | None
    parent_job_id: str | None
    worker_id: str | None = None
    queue_slot: int | None = None
    modal_function_call_id: str | None = None


async def heartbeat_worker_job(
    job_id: str,
    *,
    pending_failure_count: int = 0,
    pending_last_error: str | None = None,
) -> None:
    await get_db_proxy().heartbeat(
        job_id=job_id,
        pending_failure_count=pending_failure_count,
        pending_last_error=pending_last_error,
    )


async def claim_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int | None,
    modal_function_call_id: str | None = None,
) -> ClaimedWorkerJob | None:
    row = await get_db_proxy().claim_job(
        queue_key=queue_key,
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )
    if row is None:
        return None
    return ClaimedWorkerJob(
        id=row.id,
        kind=row.kind,
        queue_key=row.queue_key,
        subject_table=row.subject_table,
        subject_id=row.subject_id,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        org_id=row.org_id,
        parent_job_id=row.parent_job_id,
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )


async def _record_outcome(
    *,
    job_id: str,
    outcome: JobOutcome,
    attempts: int,
    max_attempts: int,
    kind: WorkerJobKind | None = None,
    subject_table: str | None = None,
    subject_id: str | None = None,
) -> None:
    """Transition the claimed `worker_jobs` row to its terminal state.

    Success → status=SUCCESS, merge ``result_summary``, stamp
    ``finished_at``.
    Retryable failure with attempts remaining → status=RETRYING,
    stamp ``error_message``, clear claim metadata.
    Non-retryable (or retries exhausted) → status=FAILED.
    """
    proxy = get_db_proxy()
    if outcome.success is not None:
        import json

        summary = outcome.success.result_summary
        await proxy.record_success(
            job_id=job_id,
            summary_json=json.dumps(summary) if summary is not None else None,
        )
        return

    assert outcome.failure is not None
    retry = outcome.failure.retryable and attempts < max_attempts
    if retry:
        retry_at: datetime | None = None
        retry_reason = classify_retry_reason(outcome.failure.error_message)
        delay_seconds: float | None = None
        if kind == WorkerJobKind.TRIAL:
            delay_seconds = calculate_trial_retry_delay_seconds(
                attempts=attempts,
                error_message=outcome.failure.error_message,
            )
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

        await proxy.record_retry(
            job_id=job_id,
            error_message=outcome.failure.error_message,
            retry_at=retry_at,
            mirror_to_trials=(
                kind == WorkerJobKind.TRIAL and subject_table == "trials"
            ),
            subject_id=subject_id,
        )
        console.print(
            f"metric=worker_job_retry_requeued id={job_id} "
            f"attempts={attempts}/{max_attempts} "
            f"retry_reason={retry_reason} "
            f"retry_delay_seconds={delay_seconds or 0:.2f}"
        )
    else:
        await proxy.record_failure(
            job_id=job_id, error_message=outcome.failure.error_message
        )


async def run_single_worker_job(
    queue_key: str,
    *,
    worker_id: str,
    queue_slot: int,
    modal_function_call_id: str | None = None,
    post_success_hooks: PostSuccessHooks | None = None,
) -> bool:
    """Claim and execute at most one `worker_jobs` row.

    Returns ``True`` if a row was claimed (regardless of the handler's
    outcome), ``False`` if the queue was empty. Exceptions from the
    handler are caught and reported through the outcome pipeline so the
    row never gets stuck in ``RUNNING``; only ``asyncio.CancelledError``
    propagates so Modal worker cancellation still unwinds cleanly.

    ``post_success_hooks`` fires after a SUCCESS has been durably
    recorded on the ``worker_jobs`` row. Hook exceptions are logged but
    do not fail the job -- they're operator notifications, not
    correctness-critical.
    """
    _ensure_handlers_registered()

    job = await claim_single_worker_job(
        queue_key,
        worker_id=worker_id,
        queue_slot=queue_slot,
        modal_function_call_id=modal_function_call_id,
    )
    if job is None:
        return False

    console.print(
        f"[cyan]Processing worker_job id={job.id} kind={job.kind.value} "
        f"(queue_key={queue_key}, attempt={job.attempts}/{job.max_attempts})[/cyan]"
    )

    try:
        handler = get_handler(job.kind)
    except NoHandlerRegisteredError as exc:
        # Fail the row instead of leaving it in RUNNING so cleanup
        # doesn't have to reap it via the stale-heartbeat sweep.
        await _record_outcome(
            job_id=job.id,
            outcome=JobOutcome.fail(
                f"No handler registered for kind={job.kind.value!r}: {exc}",
                retryable=False,
            ),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            kind=job.kind,
            subject_table=job.subject_table,
            subject_id=job.subject_id,
        )
        return True

    try:
        # Handlers receive the claimed projection; they can hydrate a
        # full ORM row if they need more columns.
        outcome = await handler.run(job)  # type: ignore[arg-type]
    except asyncio.CancelledError:
        console.print(f"[yellow]worker_job {job.id} cancelled[/yellow]")
        raise
    except Exception as exc:  # handler-raised exceptions are retryable by default
        console.print(f"[red]worker_job {job.id} handler error: {exc!r}[/red]")
        outcome = JobOutcome.fail(f"{type(exc).__name__}: {exc}", retryable=True)

    if (outcome.success is None) == (outcome.failure is None):
        # Defensive: the dataclass enforces this, but double-check so a
        # buggy handler can't leave a row RUNNING.
        outcome = JobOutcome.fail(
            "handler returned an invalid JobOutcome",
            retryable=False,
        )

    status = WorkerJobStatus.SUCCESS if outcome.success else WorkerJobStatus.FAILED
    console.print(
        f"[dim]worker_job {job.id} -> {status.value} "
        f"(kind={job.kind.value}, queue_key={queue_key})[/dim]"
    )

    await _record_outcome(
        job_id=job.id,
        outcome=outcome,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        kind=job.kind,
        subject_table=job.subject_table,
        subject_id=job.subject_id,
    )

    if outcome.success is not None and post_success_hooks and job.subject_id:
        hook = post_success_hooks.get(job.kind)
        if hook is not None:
            try:
                await hook(job.subject_id)
            except Exception as exc:
                console.print(
                    f"[yellow]post-success hook for kind={job.kind.value} "
                    f"job={job.id} failed: {exc}[/yellow]"
                )

    return True
