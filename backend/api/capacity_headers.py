"""Advertise a recommended submission ceiling on submit/sweep/upload responses.

Inert on the client (extra response headers only; no body change). Reads the
~5s-cached load snapshot; never fails the request if the snapshot is unavailable.
Also maintains a process-local EWMA of submission latency, persisted (throttled)
into queue_runtime_status so the snapshot's pressure term can read it back.
"""

from __future__ import annotations

import time

from starlette.requests import Request

from oddish.core.admin import (
    SUBMIT_CONCURRENCY_HEADER,
    SUBMIT_LATENCY_COMPONENT,
    get_cached_load_snapshot,
)

# Prefix-matched so /tasks/sweep and /tasks/sweep/batch both qualify.
_CAPACITY_PATHS = (
    "/tasks/sweep",
    "/tasks/upload/init",
    "/tasks/upload/complete",
)

_SWEEP_RTT_ALPHA = 0.2
_SWEEP_RTT_PERSIST_INTERVAL_S = 5.0
_sweep_rtt_ewma: float | None = None
_sweep_rtt_persisted_at: float = 0.0
_monotonic = time.monotonic


def _is_capacity_path(path: str) -> bool:
    return any(path.startswith(p) for p in _CAPACITY_PATHS)


def _update_sweep_rtt_ewma(latency_s: float) -> float:
    global _sweep_rtt_ewma
    if _sweep_rtt_ewma is None:
        _sweep_rtt_ewma = latency_s
    else:
        _sweep_rtt_ewma = (
            _SWEEP_RTT_ALPHA * latency_s + (1.0 - _SWEEP_RTT_ALPHA) * _sweep_rtt_ewma
        )
    return _sweep_rtt_ewma


async def _maybe_persist_sweep_rtt(value: float) -> None:
    """Upsert the EWMA at most once per interval per container (best-effort)."""
    global _sweep_rtt_persisted_at
    now = _monotonic()
    if now - _sweep_rtt_persisted_at < _SWEEP_RTT_PERSIST_INTERVAL_S:
        return
    _sweep_rtt_persisted_at = now
    from oddish.workers.queue.runtime_status import record_queue_runtime_status

    # `sweep_rtt_p95_ewma` is a contract-mandated key name: `value` is actually a
    # smoothed MEAN of submission-handler latency (the EWMA above, not a true p95)
    # and pools /tasks/sweep with /tasks/upload/*. Kept verbatim so the snapshot reads it.
    await record_queue_runtime_status(
        SUBMIT_LATENCY_COMPONENT, {"sweep_rtt_p95_ewma": value}
    )


async def capacity_header_middleware(request: Request, call_next):
    started = _monotonic()
    response = await call_next(request)
    if not _is_capacity_path(request.url.path):
        return response
    # Record submission latency into the EWMA (best-effort; never break a request).
    try:
        ewma = _update_sweep_rtt_ewma(_monotonic() - started)
        await _maybe_persist_sweep_rtt(ewma)
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = await get_cached_load_snapshot()
    except Exception:  # noqa: BLE001 - advertising is best-effort
        return response
    ceiling = snap.submit_ceiling
    response.headers[SUBMIT_CONCURRENCY_HEADER] = (
        f"ceiling={ceiling}; pressure={snap.pressure:.2f}; ttl={snap.ttl_seconds}"
    )
    response.headers["RateLimit-Policy"] = (
        f"submit;q={ceiling};qu=concurrent-requests;w=0"
    )
    response.headers["RateLimit"] = f"submit;r={ceiling};t={snap.ttl_seconds}"
    return response
