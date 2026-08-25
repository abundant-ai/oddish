"""Optional OpenTelemetry (Logfire) tracing for the portable oddish layer.

Mirrors ``backend/observability.py``'s ``span`` / ``configure`` but lives in the
``oddish`` package (which must not import ``backend/``) and treats logfire as
OPTIONAL: when logfire isn't installed or no ``LOGFIRE_TOKEN`` is set, everything
degrades to a no-op. So the standalone off-Modal dispatcher (``oddish.dispatch.
runner``) emits traces when observability is available and runs unchanged when it
isn't. On Modal the backend configures logfire via ``backend/observability``; this
shim shares the same in-process logfire singleton.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from oddish.db import WorkerJobKind, WorkerJobStatus

logger = logging.getLogger(__name__)

_configured = False
_lock = threading.Lock()
_worker_job_transitions_counter = None
_worker_job_duration_histogram = None
_queue_jobs_gauge = None
_queue_slots_gauge = None
_dispatch_workers_spawned_counter = None
_dispatch_cycles_counter = None
_dispatch_duration_histogram = None
_last_dispatch_queue_keys: set[str] = set()

_AGGREGATE_QUEUE_KEY = "__all__"
DispatchCycleOutcome = Literal["success", "skipped", "error"]


def configure_observability(service_name: str) -> bool:
    """Initialize Logfire for a standalone oddish process (e.g. the off-Modal
    dispatcher). No-op returning False when logfire is unavailable or no
    ``LOGFIRE_TOKEN`` is set. Idempotent.
    """
    global _configured
    with _lock:
        if _configured:
            return True
        if not os.environ.get("LOGFIRE_TOKEN"):
            logger.info(
                "LOGFIRE_TOKEN not set; oddish tracing disabled (%s)", service_name
            )
            return False
        try:
            import logfire
        except ImportError:
            logger.info("logfire not installed; oddish tracing disabled")
            return False
        configure_kwargs: dict = dict(
            service_name=service_name,
            service_version=os.environ.get("ODDISH_RELEASE")
            or os.environ.get("GIT_COMMIT_SHA"),
            send_to_logfire="if-token-present",
            console=False,
        )
        try:
            configure_kwargs["advanced"] = logfire.AdvancedOptions(
                exception_callback=classify_recorded_exception
            )
        except Exception:
            logger.warning("logfire AdvancedOptions unavailable", exc_info=True)
        try:
            logfire.configure(**configure_kwargs)
        except Exception:
            logger.warning("logfire.configure failed", exc_info=True)
            return False
        # Auto-instrument the DB/HTTP the dispatch cycle uses so its work nests
        # under the explicit ``dispatch.cycle`` span (see ``span`` below).
        for name in (
            "instrument_asyncpg",
            "instrument_httpx",
            "instrument_system_metrics",
        ):
            fn = getattr(logfire, name, None)
            if fn is None:
                continue
            try:
                if name == "instrument_httpx":
                    # Both hook params: async clients only run the async hook.
                    fn(
                        response_hook=expected_4xx_response_hook,
                        async_response_hook=expected_4xx_response_hook,
                    )
                else:
                    fn()
            except Exception:
                logger.warning("logfire %s failed", name, exc_info=True)
        _configured = True
        logger.info("oddish tracing configured (service=%s)", service_name)
        return True


# ---------------------------------------------------------------------------
# Severity policy: handled 4xx responses and expected vendor NotFounds are
# recorded, but not as errors — ``level >= error`` should mean a real failure.
# Shared by this module's configure and ``backend/observability.py``.
# ---------------------------------------------------------------------------

# Daytona SDK spans (``daytona/_utils/otel_decorator.py``) set ERROR status
# and record the raw NotFoundException on the span BEFORE oddish's own
# ``except NotFoundException`` handlers run, so no call-site handling can fix
# their severity. A missing sandbox on get/delete is the expected outcome for
# finished or harvested trials (teardown and the reaper both classify it as
# success); NotFound on any other SDK operation stays an error.
_DAYTONA_EXPECTED_NOTFOUND_SPANS = frozenset(
    {"AsyncDaytona.get", "AsyncDaytona.delete"}
)

_WARN_LEVEL_NUM = 13  # logfire's numeric "warn" level


def _set_span_status_ok(span) -> None:
    """ERROR -> OK. OK rather than UNSET: the OTel SDK silently ignores
    ``set_status(UNSET)``, so OK is the only way to un-error a span."""
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.OK))
    except Exception:  # noqa: BLE001 - observability must never raise
        pass


def expected_4xx_response_hook(span, request, response) -> None:
    """httpx response hook: un-error client spans for handled 4xx responses.

    The OTel httpx instrumentation marks EVERY client response >= 400 as
    ERROR (server spans get the 4xx exemption; client spans do not), so a
    Clerk 404 on a background recheck or a Daytona 404 on a gone sandbox
    painted dashboards red while the caller handled it. This hook runs after
    the instrumentation assigns the status and downgrades 4xx to OK at warn
    level; 5xx stays an error. A caller that treats a 4xx as fatal raises,
    and that exception is recorded on the calling span as usual.
    """
    try:
        status_code = getattr(response, "status_code", None)
        if status_code is None or not 400 <= int(status_code) < 500:
            return
        _set_span_status_ok(span)
        span.set_attributes({"logfire.level_num": _WARN_LEVEL_NUM})
    except Exception:  # noqa: BLE001
        pass


def classify_recorded_exception(helper) -> None:
    """``logfire.AdvancedOptions.exception_callback``: downgrade recorded
    exceptions that represent handled, expected outcomes.

    Module-level on purpose (logfire documents the callback must be
    importable, e.g. for forked workers). logfire wraps the call in its own
    ``handle_internal_errors``, and every branch here is defensive too, so a
    bug reverts to default severity instead of breaking a request.
    """
    try:
        exc = helper.exception

        # Handled HTTP 4xx raised inside a route handler: FastAPI converts
        # it into a clean response, so recording it as an error was pure
        # labeling. Two recording paths land here. logfire's FastAPI
        # integration records an event on the request span (marked by the
        # attribute below) — drop that event and touch nothing else, the
        # request span's own 4xx handling is already correct. Auto-tracing's
        # ``Calling api.routers.*`` span records the exception as escaped
        # with ERROR status and error level already applied — undo both.
        try:
            from starlette.exceptions import HTTPException
        except ImportError:  # pragma: no cover - workers without starlette
            HTTPException = None
        if HTTPException is not None and isinstance(exc, HTTPException):
            if int(getattr(exc, "status_code", 500)) >= 500:
                return
            helper.create_issue = False
            if helper.event_attributes.get("recorded_by_logfire_fastapi"):
                helper.no_record_exception()
                return
            helper.level = "warn"
            _set_span_status_ok(helper.span)
            return

        # Expected Daytona sandbox-gone (see the span allowlist above).
        exc_type = type(exc)
        if exc_type.__name__ in ("NotFoundException", "DaytonaNotFoundError") and (
            "daytona" in (exc_type.__module__ or "")
        ):
            if getattr(helper.span, "name", None) in _DAYTONA_EXPECTED_NOTFOUND_SPANS:
                helper.create_issue = False
                helper.level = "warn"
                _set_span_status_ok(helper.span)
    except Exception:  # noqa: BLE001
        pass


def mark_observability_configured() -> None:
    """Record that another package configured the shared Logfire singleton.

    The hosted backend configures Logfire before importing Oddish worker
    modules. This hook lets portable Oddish logging helpers use that existing
    configuration without importing the hosted package or configuring Logfire
    a second time.
    """
    global _configured
    with _lock:
        _configured = True


def record_worker_job_transition(
    *,
    kind: WorkerJobKind,
    outcome: WorkerJobStatus,
    queue_key: str,
    execution_lane: str,
    duration_seconds: float,
) -> None:
    """Record one accepted ``worker_jobs`` state transition.

    Call this only after the guarded database update changes one row. The
    attributes deliberately exclude job, trial, organization, user, and error
    values so the metric cannot create one time series per record.
    """
    global _worker_job_transitions_counter, _worker_job_duration_histogram
    if not _configured:
        return

    try:
        import logfire

        with _lock:
            if _worker_job_transitions_counter is None:
                _worker_job_transitions_counter = logfire.metric_counter(
                    "oddish.worker_job.transitions",
                    unit="{transition}",
                    description="Accepted durable worker-job state transitions",
                )
            if _worker_job_duration_histogram is None:
                _worker_job_duration_histogram = logfire.metric_histogram(
                    "oddish.worker_job.duration",
                    unit="s",
                    description=(
                        "Seconds from a worker-job claim to its accepted durable "
                        "state transition"
                    ),
                )
    except Exception:
        logger.warning("failed to create worker-job metrics", exc_info=True)
        return

    attributes = {
        "kind": kind.value,
        "outcome": outcome.value,
        "queue_key": queue_key,
        "execution_lane": execution_lane,
    }
    try:
        _worker_job_transitions_counter.add(1, attributes)
    except Exception:
        logger.warning("failed to record worker-job transition metric", exc_info=True)
    try:
        _worker_job_duration_histogram.record(duration_seconds, attributes)
    except Exception:
        logger.warning("failed to record worker-job duration metric", exc_info=True)


def record_dispatch_snapshot(
    *,
    queue_keys: Sequence[str],
    queued_by_queue: Mapping[str, int],
    running_by_queue_key: Mapping[str, int],
    held_by_queue_key: Mapping[str, int],
    concurrency_limits: Mapping[str, int],
) -> None:
    """Record the queue counts and slot values already present in a dispatch plan.

    ``queue_key="__all__"`` is the aggregate series. Every discovered queue key
    also receives queued, running, held, and limit observations, using zero for
    a value absent from one of the plan's mappings. A queue that disappears from
    the next plan receives one final zero for jobs and held slots.
    """
    global _queue_jobs_gauge, _queue_slots_gauge, _last_dispatch_queue_keys
    if not _configured:
        return

    try:
        import logfire

        with _lock:
            if _queue_jobs_gauge is None:
                _queue_jobs_gauge = logfire.metric_gauge(
                    "oddish.queue.jobs",
                    unit="{job}",
                    description="Worker-job rows observed by the dispatcher",
                )
            if _queue_slots_gauge is None:
                _queue_slots_gauge = logfire.metric_gauge(
                    "oddish.queue.slots",
                    unit="{slot}",
                    description=(
                        "Held queue-slot leases and configured concurrency limits"
                    ),
                )
    except Exception:
        logger.warning("failed to create dispatch snapshot metrics", exc_info=True)
        return

    current_queue_keys = (
        set(queue_keys)
        | set(queued_by_queue)
        | set(running_by_queue_key)
        | set(held_by_queue_key)
        | set(concurrency_limits)
    )
    with _lock:
        departed_queue_keys = _last_dispatch_queue_keys - current_queue_keys
        observed_queue_keys = sorted(current_queue_keys | departed_queue_keys)
        job_observations = [
            (
                sum(queued_by_queue.values()),
                {"state": "queued", "queue_key": _AGGREGATE_QUEUE_KEY},
            ),
            (
                sum(running_by_queue_key.values()),
                {"state": "running", "queue_key": _AGGREGATE_QUEUE_KEY},
            ),
        ]
        slot_observations = [
            (
                sum(held_by_queue_key.values()),
                {"state": "held", "queue_key": _AGGREGATE_QUEUE_KEY},
            ),
            (
                sum(concurrency_limits.values()),
                {"state": "limit", "queue_key": _AGGREGATE_QUEUE_KEY},
            ),
        ]
        for queue_key in observed_queue_keys:
            job_observations.extend(
                (
                    (
                        queued_by_queue.get(queue_key, 0),
                        {"state": "queued", "queue_key": queue_key},
                    ),
                    (
                        running_by_queue_key.get(queue_key, 0),
                        {"state": "running", "queue_key": queue_key},
                    ),
                )
            )
        for queue_key in observed_queue_keys:
            slot_observations.append(
                (
                    held_by_queue_key.get(queue_key, 0),
                    {"state": "held", "queue_key": queue_key},
                )
            )
            if queue_key in current_queue_keys:
                slot_observations.append(
                    (
                        concurrency_limits.get(queue_key, 0),
                        {"state": "limit", "queue_key": queue_key},
                    )
                )

        failed_departed_queue_keys: set[str] = set()
        for value, attributes in job_observations:
            try:
                _queue_jobs_gauge.set(value, attributes)
            except Exception:
                queue_key = attributes["queue_key"]
                if queue_key in departed_queue_keys:
                    failed_departed_queue_keys.add(queue_key)
                logger.warning("failed to record queue-jobs metric", exc_info=True)
        for value, attributes in slot_observations:
            try:
                _queue_slots_gauge.set(value, attributes)
            except Exception:
                queue_key = attributes["queue_key"]
                if queue_key in departed_queue_keys:
                    failed_departed_queue_keys.add(queue_key)
                logger.warning("failed to record queue-slots metric", exc_info=True)

        _last_dispatch_queue_keys = current_queue_keys | failed_departed_queue_keys


def record_dispatch_cycle(
    *,
    workers_spawned: int,
    spawn_cap_reached: bool,
    duration_seconds: float,
    outcome: DispatchCycleOutcome,
) -> None:
    """Record one dispatcher cycle and spawns from fully successful cycles.

    ``skipped`` means a recognized transient condition, currently ``OSError``,
    prevented completion and the polling host will retry. ``error`` is reserved
    for unexpected failures. The workers-spawned counter remains limited to
    ``success`` cycles because a failed fan-out does not expose a trustworthy
    partial spawn count on every dispatcher host.
    """
    global _dispatch_workers_spawned_counter
    global _dispatch_cycles_counter, _dispatch_duration_histogram
    if not _configured:
        return

    try:
        import logfire

        with _lock:
            if _dispatch_workers_spawned_counter is None:
                _dispatch_workers_spawned_counter = logfire.metric_counter(
                    "oddish.dispatch.workers_spawned",
                    unit="{worker}",
                    description="Workers spawned by successful dispatch cycles",
                )
            if _dispatch_cycles_counter is None:
                _dispatch_cycles_counter = logfire.metric_counter(
                    "oddish.dispatch.cycles",
                    unit="{cycle}",
                    description=(
                        "Successful, transiently skipped, and failed dispatcher "
                        "cycles"
                    ),
                )
            if _dispatch_duration_histogram is None:
                _dispatch_duration_histogram = logfire.metric_histogram(
                    "oddish.dispatch.duration",
                    unit="s",
                    description="Dispatcher cycle duration in seconds",
                )
    except Exception:
        logger.warning("failed to create dispatch-cycle metrics", exc_info=True)
        return

    attributes = {
        "outcome": outcome,
        "spawn_cap_reached": spawn_cap_reached,
    }
    observations = [
        (_dispatch_cycles_counter.add, 1),
        (_dispatch_duration_histogram.record, duration_seconds),
    ]
    if outcome == "success":
        observations.append((_dispatch_workers_spawned_counter.add, workers_spawned))
    for observe, value in observations:
        try:
            observe(value, attributes)
        except Exception:
            logger.warning("failed to record dispatch-cycle metric", exc_info=True)


def span(name: str, /, **attributes):
    """Open a Logfire span, or a no-op context manager when tracing is off.

    Use at portable-layer entry points (the off-Modal dispatch cycle) so the
    auto-instrumented DB/HTTP children nest under a named parent instead of
    floating at the trace root. Gated on ``configure_observability`` having run
    so an unconfigured logfire never warns on the hot path.
    """
    if _configured:
        try:
            import logfire

            return logfire.span(name, **attributes)
        except Exception:
            logger.warning("logfire.span(%r) failed", name, exc_info=True)
    return nullcontext()


def log_warning(
    message: str,
    *,
    tags: tuple[str, ...] = (),
    **attributes,
) -> None:
    """Emit a warning to process logs and, when configured, Logfire.

    Standard logging remains the durable fallback for self-hosted installs.
    The direct Logfire record is structured so alert rules can group/filter by
    attributes such as ``model`` instead of parsing console text.
    """
    rendered_attributes = " ".join(
        f"{key}={value!r}" for key, value in sorted(attributes.items())
    )
    logger.warning(
        "%s%s",
        message,
        f" {rendered_attributes}" if rendered_attributes else "",
    )
    if not _configured:
        return
    try:
        import logfire

        logfire.warning(message, _tags=list(tags) or None, **attributes)
    except Exception:
        logger.warning("logfire.warning(%r) failed", message, exc_info=True)


def log_unpriced_trial_if_needed(
    *,
    cost_usd: float | None,
    trial_id: str,
    model: str | None,
    agent: str | None,
    provider: str | None,
    attempt: int,
    input_tokens: int | None,
    cache_tokens: int | None,
    cache_write_tokens: int | None,
    output_tokens: int | None,
    native_cost_usd: float | None,
    native_cost_trusted: bool,
) -> bool:
    """Log one structured integrity warning when tokens cannot be priced."""
    if cost_usd is not None or not any(
        int(tokens or 0) > 0
        for tokens in (
            input_tokens,
            cache_tokens,
            cache_write_tokens,
            output_tokens,
        )
    ):
        return False

    log_warning(
        "Trial has token usage but no resolved cost",
        tags=("cost-integrity", "unpriced-model"),
        metric="trial_cost_unpriced",
        trial_id=trial_id,
        model=model or "unknown",
        agent=agent or "unknown",
        provider=provider or "unknown",
        attempt=attempt,
        input_tokens=input_tokens or 0,
        cache_tokens=cache_tokens or 0,
        cache_write_tokens=cache_write_tokens or 0,
        output_tokens=output_tokens or 0,
        native_cost_usd=native_cost_usd,
        native_cost_trusted=native_cost_trusted,
    )
    return True


def log_missing_trial_metering_if_needed(
    *,
    cost_usd: float | None,
    trial_id: str,
    model: str | None,
    agent: str | None,
    provider: str | None,
    attempt: int,
    input_tokens: int | None,
    cache_tokens: int | None,
    cache_write_tokens: int | None,
    output_tokens: int | None,
    native_cost_usd: float | None,
    has_execution_evidence: bool,
) -> bool:
    """Warn when a likely-billable Bedrock run has no usable metering.

    Claude Code normally exposes Bedrock usage through its streamed assistant
    messages and Harbor's final result. If both sources are empty, token-based
    pricing cannot distinguish a free pre-inference failure from lost usage.
    Limit the warning to Bedrock Claude Code runs with independent evidence
    that agent execution produced a trajectory.
    """
    if (
        not has_execution_evidence
        or "claude-code" not in (agent or "").strip().lower()
        or (provider or "").strip().lower() != "bedrock"
        or (cost_usd is not None and cost_usd > 0)
        or any(
            int(tokens or 0) > 0
            for tokens in (
                input_tokens,
                cache_tokens,
                cache_write_tokens,
                output_tokens,
            )
        )
    ):
        return False

    log_warning(
        "Bedrock trial produced execution output but no token or cost metering",
        tags=("cost-integrity", "missing-metering"),
        metric="trial_cost_missing_metering",
        trial_id=trial_id,
        model=model or "unknown",
        agent=agent or "unknown",
        provider=provider or "unknown",
        attempt=attempt,
        native_cost_usd=native_cost_usd,
    )
    return True
