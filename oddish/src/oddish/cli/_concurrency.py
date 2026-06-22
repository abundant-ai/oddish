"""Adaptive client-side concurrency control for task submission.

The CLI uploads and submits batches of tasks against a shared API. A fixed
worker count is a poor fit: too low and large batches crawl, too high and a
busy API starts shedding load. This module provides a gradient2 in-flight
limiter (Netflix/Envoy "adaptive concurrency") that discovers a safe operating
point on its own from request latency -- it nudges its ceiling toward
``limit * gradient + queue_size`` on each clean sample, where ``gradient``
compares a no-load RTT floor against the live RTT, and it still backs off hard
and multiplicatively the moment the server signals explicit backpressure (HTTP
429/500/502/503/504, request timeouts, or a slow connection-pool checkout).

The gradient law follows Netflix's ``concurrency-limits`` ``Gradient2Limit`` and
Envoy's adaptive-concurrency filter, with one deliberate change: the no-load
baseline is a windowed MINIMUM (re-probed as samples expire), not a long EWMA.
An average baseline drifts up toward the live RTT under sustained load, driving
the gradient to 1.0 and blinding the limiter exactly when the server is
saturated; a rolling minimum keeps the floor honest. The server can also
advertise a recommended ceiling (the ``Oddish-Submit-Concurrency`` response
header); the limiter clamps its maximum to that advice.

The pieces here are pure -- no network calls, no thread pools -- so the control
logic is fully unit-testable. ``ConcurrencyGate`` / ``map_with_adaptive_concurrency``
(added alongside the pool wiring) build the runtime throttle on top.
"""

from __future__ import annotations

import contextvars
import math
import os
import threading
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar, cast

import httpx

# Bounds for the adaptive in-flight limit. A floor of 4 keeps throughput off the
# floor for large batches; the ceiling matches the server's advertised maximum
# (``core.admin.CLIENT_CEILING_MAX``) so an idle backend lets the client widen
# well past the old static band, while a busy backend pulls the advertised
# ceiling down and the limiter follows it.
DEFAULT_MIN_CONCURRENCY = 4
DEFAULT_MAX_CONCURRENCY = 64

# Gentle multiplicative-decrease factor for the hard backpressure override. 0.9
# maximizes throughput for a single client protecting a downstream; 0.5 is the
# hard floor so one backpressure step never cuts the limit by more than half.
# This matches AIMDLimit's enforced ``[0.5, 1.0)`` backoff range.
DEFAULT_BACKOFF_FACTOR = 0.9
MIN_BACKOFF_FACTOR = 0.5

# Gradient2 tuning. ``RTT_TOLERANCE`` (>1) lets the live RTT rise modestly above
# the no-load floor before the gradient starts shrinking the limit; the gradient
# is clamped to ``[0.5, 1.0]`` (a single sample never more than halves the
# target, and a faster-than-floor sample never pushes the gradient above 1).
# ``GRADIENT_SMOOTHING`` blends each new target into the current limit so one
# noisy sample can't swing it. ``NOLOAD_WINDOW`` sizes the sliding window for the
# no-load minimum -- large enough to retain the floor across a submit batch,
# small enough that the floor re-probes over a long run.
RTT_TOLERANCE = 1.5
GRADIENT_MIN = 0.5
GRADIENT_MAX = 1.0
GRADIENT_SMOOTHING = 0.2
NOLOAD_WINDOW = 100

# Transient statuses that signal server backpressure. Identical to the retry set
# used for the idempotent upload calls (api._RETRY_STATUS_CODES). Deterministic
# 4xx (400/401/403/404/409/422) are client errors -- replaying or backing off
# won't help -- so they must NOT shrink the limit.
BACKPRESSURE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Env override for the adaptive API limit. An integer pins the limit (disables
# adaptation); unset / non-numeric leaves it adaptive. The ``--submit-concurrency``
# CLI flag takes precedence over this.
SUBMIT_CONCURRENCY_ENV_VAR = "ODDISH_TASK_UPLOAD_CONCURRENCY"

# A separate, smaller ceiling for the S3 presigned-PUT step. The object store is
# a different service from the API, so its upload concurrency is bounded
# independently and is never fed by (and never feeds) the API backpressure
# signal. Clamped to a small band so it stays well under the API limit.
DEFAULT_S3_PUT_CONCURRENCY = 4
MIN_S3_PUT_CONCURRENCY = 1
MAX_S3_PUT_CONCURRENCY = 6
S3_PUT_CONCURRENCY_ENV_VAR = "ODDISH_TASK_S3_UPLOAD_CONCURRENCY"

T = TypeVar("T")
R = TypeVar("R")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_gradient_limit(
    limit: float,
    *,
    rtt_noload: float,
    rtt_actual: float,
    rtt_tolerance: float = RTT_TOLERANCE,
    smoothing: float = GRADIENT_SMOOTHING,
) -> float:
    """Return the smoothed next limit from one clean latency sample (gradient2).

    ``gradient = clamp(rtt_tolerance * rtt_noload / rtt_actual, 0.5, 1.0)`` is the
    ratio of the no-load RTT floor to the live RTT (so it shrinks below 1 as the
    server slows); ``queue_size = ceil(sqrt(limit))`` is Little's-Law headroom;
    the raw target is ``limit * gradient + queue_size`` and the result blends it
    into the current limit by ``smoothing`` so a single noisy sample can't swing
    it. Pure -- the caller applies clamps and the app-limited guard.
    """
    gradient = _clamp(
        rtt_tolerance * rtt_noload / rtt_actual, GRADIENT_MIN, GRADIENT_MAX
    )
    queue_size = math.ceil(math.sqrt(max(1.0, limit)))
    new_target = limit * gradient + queue_size
    return (1.0 - smoothing) * limit + smoothing * new_target


class AdaptiveConcurrencyLimiter:
    """A gradient2 in-flight limiter.

    ``record_sample(rtt)`` feeds one clean API-latency sample through the
    gradient2 law (:func:`compute_gradient_limit`) to nudge the limit toward a
    latency-derived target; ``record_backpressure`` is the kept hard override --
    it multiplies the limit by ``backoff_factor`` (floor ``0.5x``) the moment the
    server signals explicit backpressure (429/5xx/timeout/pool checkout), without
    waiting for the gradient. The limit is held as a float internally so repeated
    moves don't get stuck on integer rounding, and exposed as an int via
    :attr:`limit`.

    The effective maximum is ``min(max_limit, advertised_max)`` where
    ``advertised_max`` is the server-advertised ceiling (the
    ``Oddish-Submit-Concurrency`` header) -- ``None`` until one is seen, after
    which a busy backend can clamp the client below ``max_limit``. The floor
    always stays ``>= min_limit``. Setting ``min_limit == max_limit`` pins the
    limit (no adaptation) -- used when a caller supplies an explicit value.
    """

    def __init__(
        self,
        *,
        min_limit: int = DEFAULT_MIN_CONCURRENCY,
        max_limit: int = DEFAULT_MAX_CONCURRENCY,
        initial_limit: int | None = None,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        rtt_tolerance: float = RTT_TOLERANCE,
        smoothing: float = GRADIENT_SMOOTHING,
        latency_monitor: _LatencyMonitor | None = None,
    ) -> None:
        if min_limit < 1:
            raise ValueError(f"min_limit must be >= 1, got {min_limit}")
        if max_limit < min_limit:
            raise ValueError(
                f"max_limit ({max_limit}) must be >= min_limit ({min_limit})"
            )
        self.min_limit = int(min_limit)
        self.max_limit = int(max_limit)
        # Clamp the multiplier into [0.5, 1.0]: floor at 0.5 so a single step is
        # never more aggressive than a halving; cap below 1.0 so a decrease always
        # decreases.
        self.backoff_factor = _clamp(float(backoff_factor), MIN_BACKOFF_FACTOR, 0.999)
        self.rtt_tolerance = float(rtt_tolerance)
        self.smoothing = float(smoothing)
        start = self.min_limit if initial_limit is None else int(initial_limit)
        self._limit = float(_clamp(float(start), self.min_limit, self.max_limit))
        # Server-advertised upper clamp (D1 header); None until one is seen.
        self._advertised_max: float | None = None
        self._monitor = (
            latency_monitor if latency_monitor is not None else _LatencyMonitor()
        )
        # Reads/writes happen from worker threads in the runtime gate; the lock
        # keeps the float state consistent (it guards arithmetic, not any I/O).
        self._lock = threading.Lock()

    def _effective_max(self) -> float:
        if self._advertised_max is None:
            return float(self.max_limit)
        return min(float(self.max_limit), self._advertised_max)

    def _effective_limit(self) -> int:
        return int(_clamp(self._limit, float(self.min_limit), self._effective_max()))

    @property
    def limit(self) -> int:
        """The current in-flight ceiling, clamped to ``[min_limit, eff_max]``."""
        with self._lock:
            return self._effective_limit()

    @property
    def rtt_actual(self) -> float | None:
        """Short-window EWMA of observed API latency (``None`` before any sample)."""
        return self._monitor.rtt_actual()

    @property
    def rtt_noload(self) -> float | None:
        """Windowed no-load RTT floor (``None`` before any sample)."""
        return self._monitor.noload_rtt()

    def set_advertised_max(self, advertised: int | None) -> int:
        """Clamp the effective maximum to a server-advertised ceiling.

        ``None`` clears the advice (revert to the static ``max_limit``). A value
        re-clamps the current limit down immediately so a freshly-lowered server
        ceiling takes effect without waiting for the next sample. Returns the new
        effective limit.
        """
        with self._lock:
            self._advertised_max = (
                None if advertised is None else max(1.0, float(advertised))
            )
            self._limit = _clamp(
                self._limit, float(self.min_limit), self._effective_max()
            )
            return self._effective_limit()

    def record_sample(self, rtt: float, *, in_flight: int | None = None) -> int:
        """Fold one clean API-latency sample through the gradient2 law.

        Updates the no-load floor and live-RTT EWMA, then moves the limit toward
        the gradient target. A shrink always applies; a grow applies only when the
        client is actually pushing the ceiling (``in_flight * 2 >= limit``, the
        app-limited guard) so the limit doesn't creep up while idle. Returns the
        new limit.
        """
        with self._lock:
            self._monitor.observe(rtt)
            noload = self._monitor.noload_rtt()
            actual = self._monitor.rtt_actual()
            if noload is None or actual is None or actual <= 0.0:
                return self._effective_limit()
            target = compute_gradient_limit(
                self._limit,
                rtt_noload=noload,
                rtt_actual=actual,
                rtt_tolerance=self.rtt_tolerance,
                smoothing=self.smoothing,
            )
            clamped = _clamp(target, float(self.min_limit), self._effective_max())
            # App-limited guard: only let the limit GROW when the client is
            # pushing it; a shrink (degraded gradient) always applies.
            if (
                clamped > self._limit
                and in_flight is not None
                and (in_flight * 2 < self._effective_limit())
            ):
                return self._effective_limit()
            self._limit = clamped
            return self._effective_limit()

    def record_backpressure(self) -> int:
        """Hard multiplicative decrease on explicit backpressure (the kept override).

        Bypasses the gradient and cuts the limit by ``backoff_factor`` (floored at
        ``0.5x``) immediately. Returns the new limit.
        """
        with self._lock:
            self._limit = max(float(self.min_limit), self._limit * self.backoff_factor)
            return self._effective_limit()


def classify_backpressure(
    *,
    status_code: int | None = None,
    exception: BaseException | None = None,
) -> bool:
    """Decide whether a request outcome should trigger the hard MD override.

    Backpressure is signalled by a transient status (:data:`BACKPRESSURE_STATUS_CODES`)
    or a request timeout / slow connection-pool checkout (``httpx.TimeoutException``,
    which covers ``httpx.PoolTimeout``). Everything else -- deterministic 4xx,
    non-load transport errors -- is neutral. High latency is NOT classified here:
    it is the gradient's job (a rising RTT shrinks the limit via
    :meth:`AdaptiveConcurrencyLimiter.record_sample`), so a separate latency cut
    would double-count.
    """
    if exception is not None:
        # TimeoutException covers ConnectTimeout/ReadTimeout/WriteTimeout and
        # PoolTimeout (the connection pool itself is saturated == too much
        # in-flight). Other transport errors (e.g. ConnectError) aren't
        # necessarily load-driven, so they don't trigger a backoff.
        return isinstance(exception, httpx.TimeoutException)
    if status_code is not None and status_code in BACKPRESSURE_STATUS_CODES:
        return True
    return False


class _LatencyMonitor:
    """The two RTT statistics the gradient2 limiter needs.

    ``rtt_actual`` is a short-window EWMA (``alpha`` = 0.2) of recent API
    latency; ``rtt_noload`` is a rolling MINIMUM over a sliding window -- the
    no-load floor. The floor is a minimum, not an average: an average drifts up
    toward ``rtt_actual`` under sustained load, driving the gradient to 1.0 and
    blinding the limiter exactly when the server is saturated. Old samples leave
    the window as new ones arrive, so the floor is continuously re-probed and a
    transient spike can't poison it forever.
    """

    def __init__(
        self, *, alpha: float = 0.2, noload_window: int = NOLOAD_WINDOW
    ) -> None:
        self._alpha = alpha
        self._ewma: float | None = None
        self._window: deque[float] = deque(maxlen=max(1, int(noload_window)))
        self._lock = threading.Lock()

    def observe(self, sample: float) -> None:
        """Fold a latency sample (seconds) into the EWMA and the no-load window."""
        with self._lock:
            if self._ewma is None:
                self._ewma = sample
            else:
                self._ewma = self._alpha * sample + (1.0 - self._alpha) * self._ewma
            self._window.append(sample)

    def rtt_actual(self) -> float | None:
        """Short-window EWMA of observed latency, or ``None`` before any sample."""
        with self._lock:
            return self._ewma

    def noload_rtt(self) -> float | None:
        """Windowed minimum (no-load floor), or ``None`` before any sample."""
        with self._lock:
            return min(self._window) if self._window else None


SUBMIT_CONCURRENCY_HEADER = "Oddish-Submit-Concurrency"


def parse_submit_concurrency_header(value: str | None) -> int | None:
    """Parse the advertised ceiling out of an ``Oddish-Submit-Concurrency`` header.

    Header form: ``ceiling=<int>; pressure=<float>; ttl=<int>``. Returns the
    ``ceiling`` as an int, or ``None`` when the header is missing or garbled
    (the client then keeps its current advertised max -- "no advice").
    """
    if not value:
        return None
    for part in value.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.strip() == "ceiling":
            try:
                return int(raw.strip())
            except ValueError:
                return None
    return None


class _SlotOutcome:
    """Per-call outcome reported by the API layer while inside a gate slot."""

    __slots__ = ("api_calls", "api_latency", "backpressure", "advertised_ceiling")

    def __init__(self) -> None:
        self.backpressure = False
        self.api_latency = 0.0
        self.api_calls = 0
        self.advertised_ceiling: int | None = None


# Ambient handle to the active gate slot. ``ConcurrencyGate.run`` installs one
# for the duration of each unit of work; the API layer reports each request's
# latency, status, and advertised ceiling through it (see ``report_api_call``).
# A ContextVar keeps the reporting decoupled from the deep call stack (no gate
# handle to thread through upload_task / submit_sweep / _retry_request) and
# isolated per worker thread.
_CURRENT_SLOT: contextvars.ContextVar[_SlotOutcome | None] = contextvars.ContextVar(
    "oddish_concurrency_slot", default=None
)


def report_backpressure() -> None:
    """Signal API backpressure to the active gate slot (no-op outside one)."""
    slot = _CURRENT_SLOT.get()
    if slot is not None:
        slot.backpressure = True


def report_api_call(latency: float, *, backpressure: bool = False) -> None:
    """Report one API request to the active gate slot.

    ``latency`` (seconds) feeds the API-only latency signal -- callers must NOT
    include time spent on the S3 presigned PUT, which is bounded separately and
    would otherwise pollute the API latency signal. Set ``backpressure`` for
    a transient status (429/5xx) so the limiter backs off even when a retry
    ultimately succeeds. No-op outside a gate slot (e.g. the single-item path).
    """
    slot = _CURRENT_SLOT.get()
    if slot is None:
        return
    slot.api_latency += latency
    slot.api_calls += 1
    if backpressure:
        slot.backpressure = True


def report_advertised_ceiling(ceiling: int | None) -> None:
    """Report a server-advertised submission ceiling to the active gate slot.

    Callers pass the parsed ``Oddish-Submit-Concurrency`` ceiling; the gate then
    clamps the limiter's max to it. No-op outside a gate slot or for a missing
    header (``None``), so a response without the header keeps the current advice.
    """
    slot = _CURRENT_SLOT.get()
    if slot is not None and ceiling is not None:
        slot.advertised_ceiling = ceiling


class ConcurrencyGate:
    """Throttle concurrent work to a limiter's adaptive in-flight ceiling.

    Wrap each unit of work with :meth:`run`: it blocks until in-flight is below
    the current limit, runs the callable, and feeds the outcome back to the
    limiter. The outcome is reported by the API layer through
    :func:`report_api_call` / :func:`report_backpressure` /
    :func:`report_advertised_ceiling` (HTTP status + API-only latency + the
    advertised ceiling), not inferred from wall-clock time, so the S3 PUT step
    never trips the API signal. A clean latency sample feeds the gradient (grow
    or shrink); a transient status, timeout, or slow pool checkout triggers the
    hard multiplicative decrease; any other failure (deterministic 4xx, bugs) is
    neutral and leaves the limit unchanged.

    Built to drive a ``ThreadPoolExecutor`` sized to ``limiter.max_limit`` -- the
    gate, not the pool size, is the real throttle, so the limit can shrink below
    the pool size under load.
    """

    def __init__(self, limiter: AdaptiveConcurrencyLimiter) -> None:
        self._limiter = limiter
        self._cond = threading.Condition()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def _acquire(self) -> int:
        with self._cond:
            while self._in_flight >= self._limiter.limit:
                self._cond.wait()
            self._in_flight += 1
            return self._in_flight

    def _release(self) -> None:
        with self._cond:
            self._in_flight -= 1
            # The limit may have changed (grown or shrunk); wake every waiter so
            # they re-check against the current value.
            self._cond.notify_all()

    def run(self, fn: Callable[..., R], /, *args: object, **kwargs: object) -> R:
        """Run ``fn(*args, **kwargs)`` once a slot is free, recording the outcome."""
        in_flight = self._acquire()
        slot = _SlotOutcome()
        token = _CURRENT_SLOT.set(slot)
        error: BaseException | None = None
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - reclassified for the limiter, then re-raised
            error = exc
            raise
        finally:
            _CURRENT_SLOT.reset(token)
            # Latency sample is the API-only time the worker reported (excludes
            # the S3 PUT); ``None`` when the worker made no API calls.
            api_latency = slot.api_latency if slot.api_calls else None
            backpressure = slot.backpressure or classify_backpressure(exception=error)
            # Honor a server-advertised ceiling before adapting, so the gradient
            # sample lands inside the (possibly lowered) advertised band.
            if slot.advertised_ceiling is not None:
                self._limiter.set_advertised_max(slot.advertised_ceiling)
            if backpressure:
                self._limiter.record_backpressure()
            elif error is None and api_latency is not None:
                # A clean latency sample drives the gradient (grow or shrink).
                self._limiter.record_sample(api_latency, in_flight=in_flight)
            # else: a non-load failure (deterministic 4xx, a bug) or a clean call
            # with no latency sample -- neutral, so the limit is unchanged.
            self._release()


def map_with_adaptive_concurrency(
    items: Sequence[T],
    worker: Callable[[T], R],
    limiter: AdaptiveConcurrencyLimiter,
    *,
    gate: ConcurrencyGate | None = None,
    on_complete: Callable[[], None] | None = None,
) -> list[R]:
    """Apply ``worker`` to each item, throttled to the limiter's in-flight limit.

    Results are returned in input order. ``on_complete`` (if given) fires once
    per finished item -- handy for advancing a progress bar. The thread pool is
    sized to ``limiter.max_limit``; the :class:`ConcurrencyGate` does the actual
    throttling, so an adaptive limit shrinks real concurrency below the pool
    size under backpressure.
    """
    if not items:
        return []
    gate = gate if gate is not None else ConcurrencyGate(limiter)
    results: list[R | None] = [None] * len(items)
    max_workers = min(limiter.max_limit, len(items))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(gate.run, worker, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            if on_complete is not None:
                on_complete()
    return cast("list[R]", results)


def _read_int_env(env_var: str) -> int | None:
    """Read an int from ``env_var``; return ``None`` if unset/blank/non-numeric."""
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_submit_concurrency(
    explicit: int | None = None,
    *,
    env_var: str = SUBMIT_CONCURRENCY_ENV_VAR,
    min_limit: int = DEFAULT_MIN_CONCURRENCY,
    max_limit: int = DEFAULT_MAX_CONCURRENCY,
) -> AdaptiveConcurrencyLimiter:
    """Resolve the in-flight limiter for API-bound submit/upload work.

    Precedence: ``explicit`` (the ``--submit-concurrency`` flag) > ``env_var`` >
    adaptive default. An explicit value (flag or env) *pins* the limit
    (``min == max``, no adaptation); the default returns an AIMD limiter clamped
    to ``[min_limit, max_limit]`` and starting at the floor.
    """
    pinned = explicit if explicit is not None else _read_int_env(env_var)
    if pinned is not None:
        pinned = max(1, int(pinned))
        return AdaptiveConcurrencyLimiter(
            min_limit=pinned, max_limit=pinned, initial_limit=pinned
        )
    return AdaptiveConcurrencyLimiter(
        min_limit=min_limit, max_limit=max_limit, initial_limit=min_limit
    )


def resolve_s3_put_concurrency(
    explicit: int | None = None,
    *,
    env_var: str = S3_PUT_CONCURRENCY_ENV_VAR,
) -> int:
    """Resolve the bound for concurrent S3 presigned PUTs.

    Precedence: ``explicit`` > ``env_var`` > :data:`DEFAULT_S3_PUT_CONCURRENCY`,
    clamped to ``[MIN_S3_PUT_CONCURRENCY, MAX_S3_PUT_CONCURRENCY]``. This bound is
    independent of the adaptive API limiter.
    """
    value = explicit if explicit is not None else _read_int_env(env_var)
    if value is None:
        value = DEFAULT_S3_PUT_CONCURRENCY
    return int(_clamp(int(value), MIN_S3_PUT_CONCURRENCY, MAX_S3_PUT_CONCURRENCY))
