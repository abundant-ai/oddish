"""Self-tuning per-model concurrency controller (advisory, default-off).

Each reconcile cycle the controller derives a per-``queue_key`` advisory
concurrency limit and writes it to ``model_concurrency_advisory``; the dispatcher
reads the advisory limit when it is fresh and otherwise falls back to the static
``settings.get_model_concurrency`` value. The static config stays the hard
ceiling and the fallback -- the controller only ever writes the advisory table
(it never mutates the hard limit it must refresh), and a stale/missing/crashed
controller decays to today's static behavior within ``STALE_TTL_SECONDS``.

Two cooperating laws, both pure and unit-tested here:

* :func:`compute_ceiling` -- a FEED-FORWARD hard maximum derived from the
  published provider rate-limit bucket and long-window historic calibration
  (Little's-Law concurrencies ``C_rpm`` / ``C_tpm``; the binding one wins),
  recomputed on a long window so it is not shaped by the fast feedback loop.
* :func:`controller_step` -- a FEEDBACK AIMD that trims the advisory at/below the
  ceiling under queue pressure: constant additive-up gated by a sustained-demand
  counter, decisive multiplicative-down, with a latched saturation cooldown
  (separate cut/resume thresholds) so it neither accelerates nor flaps.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from sqlalchemy import text

from oddish.config import settings
from oddish.core.admin import get_queue_health_core
from oddish.core.calibration import (
    DEFAULT_CALIBRATION_WINDOW_MINUTES,
    calibrate_model_concurrency,
)
from oddish.workers.queue.runtime_status import record_queue_runtime_status
from oddish.workers.queue.shared import console

__all__ = [
    "CONCURRENCY_CONTROLLER_COMPONENT",
    "ControllerDecision",
    "compute_ceiling",
    "controller_step",
    "recompute_advisory_limits",
    "get_advisory_limits",
    "merge_advisory_over_static",
]

# --- feedback AIMD gains + hysteresis (spec defaults; retune from telemetry) ---
U_LOW = 0.50  # fill below this => idle slots, shrink
U_HIGH = 0.90  # fill at/above this (with a backlog) => bottleneck, grow
WAIT_TARGET_S = 120.0  # time-in-queue target
WAIT_HI = 1.5  # latched cut above WAIT_TARGET_S * WAIT_HI
WAIT_LO = 0.75  # latched resume only below WAIT_TARGET_S * WAIT_LO
THROTTLE_MAX = 0.05  # provider-throttle fraction that forces a cut
K_UP = 2  # constant additive increase (slots/cycle) -- no acceleration
BETA = 0.70  # decisive multiplicative decrease
N_SUSTAIN = 2  # consecutive grow-votes required before one +K_UP step
FLOOR = 1  # advisory never drops below one slot

# --- feed-forward ceiling defaults ---
DEFAULT_HEADROOM = 0.6  # fraction of the provider limit we aim to use
DEFAULT_R_CALLS = 1.0  # estimated LLM calls per trial (RPM-bound denominator)
GLOBAL_MAX = 256  # absolute per-queue advisory cap (bounds the spawn count)

# --- cadence-derived guards ---
# Recompute the feed-forward ceiling on the long calibration window so it is not
# derived from metrics already shaped by the 240s feedback loop.
CEILING_WINDOW_MINUTES = DEFAULT_CALIBRATION_WINDOW_MINUTES
# The feedback throttle signal is "recent trials": a short window.
THROTTLE_WINDOW_MINUTES = 60
# Advisory is trusted only within 3x the 240s reconcile cadence; older rows decay
# to the static limit (never trust a stale advisory).
STALE_TTL_SECONDS = 720

CONCURRENCY_CONTROLLER_COMPONENT = "concurrency_controller"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_ceiling(
    *,
    base: int,
    bucket: dict | None,
    trial_seconds: float | None,
    tokens_per_trial: float | None,
    r_calls: float = DEFAULT_R_CALLS,
    override: int | None = None,
    global_max: int = GLOBAL_MAX,
) -> int:
    """Feed-forward per-queue ceiling from the provider limit + calibration.

    ``C_rpm = rpm * trial_seconds / (60 * r_calls)`` and
    ``C_tpm = tpm * trial_seconds / (60 * tokens_per_trial)`` are both Little's-Law
    concurrencies (each carries the same ``trial_seconds/60`` residence factor),
    so ``min(C_rpm, C_tpm)`` -- the binding constraint -- is commensurable. The
    result is ``clamp(floor(min * headroom), base, global_max)``: the static
    ``base`` is a floor (never advise below the operator's static value) and
    ``global_max`` the absolute cap. With no bucket or no residence time we cannot
    derive a provider bound, so fall back to ``min(base*2, global_max)``. An
    operator override is an absolute hard cap applied last (it may pull below
    ``base``). A missing TPM denominator (low token coverage) drops the TPM bound
    and leans on RPM.
    """
    configured = bool(bucket) and trial_seconds is not None and trial_seconds > 0
    if not configured:
        ceiling = min(base * 2, global_max)
    else:
        assert bucket is not None and trial_seconds is not None
        headroom = float(bucket.get("headroom", DEFAULT_HEADROOM))
        bounds: list[float] = []
        rpm = bucket.get("rpm")
        if rpm is not None and r_calls > 0:
            bounds.append(float(rpm) * trial_seconds / (60.0 * r_calls))
        tpm = bucket.get("tpm")
        if tpm is not None and tokens_per_trial and tokens_per_trial > 0:
            bounds.append(float(tpm) * trial_seconds / (60.0 * tokens_per_trial))
        if not bounds:
            ceiling = min(base * 2, global_max)
        else:
            ceiling = int(
                _clamp(
                    math.floor(min(bounds) * headroom), float(base), float(global_max)
                )
            )
    if override is not None:
        ceiling = min(ceiling, int(override))
    return ceiling


@dataclass(frozen=True)
class ControllerDecision:
    """One cycle's decision for a queue (and the state to persist for the next)."""

    new_limit: int
    cooldown: bool
    demand: int
    error: int
    reason: str


def controller_step(
    *,
    prev_limit: int,
    cooldown: bool,
    demand: int,
    fill: float | None,
    queued: int,
    wait_p95: float | None,
    throttle_rate: float,
    ceiling: int,
) -> ControllerDecision:
    """One feedback step: trim the advisory at/below ``ceiling`` (genuine AIMD).

    Increase is constant additive (``+K_UP``, gated by ``N_SUSTAIN`` consecutive
    grow-votes -- the counter never multiplies the step); decrease is decisive
    multiplicative (``xBETA``). A latched ``cooldown`` (cut above
    ``WAIT_TARGET_S*WAIT_HI`` or on a throttle spike, resume only below
    ``WAIT_TARGET_S*WAIT_LO``) and the ``U_LOW..U_HIGH`` dead-band prevent
    -1/+1 flap; the demand counter resets on any non-grow vote (no windup).
    """
    fill_v = fill if fill is not None else 0.0
    wait_v = wait_p95 if wait_p95 is not None else 0.0

    saturated = wait_v > WAIT_TARGET_S * WAIT_HI or throttle_rate > THROTTLE_MAX
    recovered = wait_v < WAIT_TARGET_S * WAIT_LO and throttle_rate <= THROTTLE_MAX

    # 0. latched saturation cooldown (separate cut/resume thresholds).
    if saturated:
        cooldown = True
    elif recovered:
        cooldown = False
    # else: cooldown latched (unchanged)

    # 1. dead-banded error (growth latched off until well below target).
    if saturated:
        error, reason = -1, "saturated"
    elif cooldown:
        error, reason = 0, "cooldown"
    elif fill_v > 1.0:
        error, reason = -1, "oversubscribed"
    elif fill_v <= U_LOW:
        error, reason = -1, "idle"
    elif queued > 0 and fill_v >= U_HIGH:
        error, reason = 1, "bottleneck"
    else:
        error, reason = 0, "hold"

    # 2. sustained-demand counter (NOT a step multiplier).
    demand = demand + 1 if error > 0 else 0
    grow = error > 0 and demand >= N_SUSTAIN

    # 3. genuine AIMD: additive-up (constant), multiplicative-down (decisive).
    if error < 0:
        new = math.floor(prev_limit * BETA)
    elif grow:
        new = prev_limit + K_UP
    else:
        new = prev_limit

    # 4. feedback may only move the advisory at/below the feed-forward ceiling.
    new_limit = int(_clamp(new, FLOOR, ceiling))
    return ControllerDecision(
        new_limit=new_limit,
        cooldown=cooldown,
        demand=demand,
        error=error,
        reason=reason,
    )


async def _read_advisory_state(session) -> dict[str, dict]:
    """Read the current advisory rows as ``{queue_key: {limit, cooldown, demand}}``.

    Returns ``{}`` if the table does not exist yet (pre-migration) so the
    controller degrades gracefully.
    """
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT queue_key, advisory_limit, state "
                    "FROM model_concurrency_advisory"
                )
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - tolerate missing table / transient errors
        console.print(f"[yellow]Advisory state read skipped: {exc}[/yellow]")
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        state = row.state if isinstance(row.state, dict) else {}
        if isinstance(row.state, str):
            try:
                state = json.loads(row.state)
            except (TypeError, ValueError):
                state = {}
        out[row.queue_key] = {
            "advisory_limit": int(row.advisory_limit),
            "cooldown": bool(state.get("cooldown", False)),
            "demand": int(state.get("demand", 0)),
        }
    return out


async def _upsert_advisory(
    session, queue_key: str, decision: ControllerDecision
) -> None:
    """Upsert one queue's advisory limit + controller state."""
    state = json.dumps(
        {
            "cooldown": decision.cooldown,
            "demand": decision.demand,
            "last_error": decision.error,
            "last_reason": decision.reason,
        }
    )
    await session.execute(
        text(
            """
            INSERT INTO model_concurrency_advisory
                (queue_key, advisory_limit, updated_at, state)
            VALUES (:queue_key, :advisory_limit, NOW(), CAST(:state AS JSONB))
            ON CONFLICT (queue_key) DO UPDATE
            SET advisory_limit = EXCLUDED.advisory_limit,
                updated_at = NOW(),
                state = EXCLUDED.state
            """
        ),
        {
            "queue_key": queue_key,
            "advisory_limit": decision.new_limit,
            "state": state,
        },
    )


async def recompute_advisory_limits(session) -> dict[str, int]:
    """Recompute + persist every active queue's advisory limit (best-effort).

    Reads the live queue health (fill / queued / wait_p95), the long-window
    calibration (trial_seconds / tokens_per_trial for the feed-forward ceiling),
    and a recent-window throttle rate (the feedback cut signal); derives each
    queue's ceiling and feedback step; and upserts the result. Swallows its own
    errors (a controller failure must never break the reconcile loop) and returns
    ``{queue_key: new_limit}`` for the queues it updated.
    """
    try:
        health = await get_queue_health_core(session)
        ceiling_cal = {
            c.queue_key: c
            for c in await calibrate_model_concurrency(
                session, window_minutes=CEILING_WINDOW_MINUTES
            )
        }
        recent_cal = {
            c.queue_key: c
            for c in await calibrate_model_concurrency(
                session, window_minutes=THROTTLE_WINDOW_MINUTES
            )
        }
        prior = await _read_advisory_state(session)

        updated: dict[str, int] = {}
        log: list[dict] = []
        for cap in health.capacity:
            queue_key = cap.queue_key
            base = settings.get_model_concurrency(queue_key)
            normalized = settings.normalize_queue_key(queue_key)
            override = settings.model_concurrency_overrides.get(normalized)
            cal = ceiling_cal.get(queue_key)
            ceiling = compute_ceiling(
                base=base,
                bucket=settings.get_provider_rate_limit(queue_key),
                trial_seconds=cal.avg_trial_seconds if cal else None,
                tokens_per_trial=cal.r_tokens if cal else None,
                override=override,
            )
            prev = prior.get(queue_key, {})
            recent = recent_cal.get(queue_key)
            decision = controller_step(
                prev_limit=int(prev.get("advisory_limit", base)),
                cooldown=bool(prev.get("cooldown", False)),
                demand=int(prev.get("demand", 0)),
                fill=cap.fill,
                queued=cap.queued,
                wait_p95=cap.wait_p95_seconds,
                throttle_rate=recent.throttle_rate if recent else 0.0,
                ceiling=ceiling,
            )
            await _upsert_advisory(session, queue_key, decision)
            updated[queue_key] = decision.new_limit
            log.append(
                {
                    "queue_key": queue_key,
                    "old": int(prev.get("advisory_limit", base)),
                    "new": decision.new_limit,
                    "ceiling": ceiling,
                    "error": decision.error,
                    "reason": decision.reason,
                    "cooldown": decision.cooldown,
                    "demand": decision.demand,
                }
            )

        await record_queue_runtime_status(
            CONCURRENCY_CONTROLLER_COMPONENT,
            {"updated": len(updated), "decisions": log},
        )
        return updated
    except Exception as exc:  # noqa: BLE001 - controller is best-effort, never fatal
        console.print(f"[yellow]Concurrency controller skipped: {exc}[/yellow]")
        return {}


async def get_advisory_limits(
    session,
    *,
    fresh_within: float = STALE_TTL_SECONDS,
    global_max: int = GLOBAL_MAX,
) -> dict[str, int]:
    """Read fresh advisory limits as ``{queue_key: limit}`` (decay-to-static).

    Only rows updated within ``fresh_within`` seconds are returned; stale/missing
    rows are omitted so the dispatcher decays to the static limit. Each value is
    re-clamped to ``[FLOOR, global_max]`` as a belt-and-suspenders bound on the
    absolute spawn count. Returns ``{}`` if the table does not exist yet.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT queue_key, advisory_limit
                    FROM   model_concurrency_advisory
                    WHERE  updated_at >= NOW() - make_interval(secs => :fresh)
                    """
                ),
                {"fresh": float(fresh_within)},
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - tolerate missing table / transient errors
        console.print(f"[yellow]Advisory limit read skipped: {exc}[/yellow]")
        return {}

    return {
        row.queue_key: int(_clamp(int(row.advisory_limit), FLOOR, global_max))
        for row in rows
    }


def merge_advisory_over_static(
    static: dict[str, int], advisory: dict[str, int]
) -> dict[str, int]:
    """Overlay fresh advisory limits onto the static per-queue limits.

    Advisory wins for an active queue (a key present in ``static``); an
    advisory-only key (a queue not active this cycle) is ignored, and an
    un-advised active queue keeps its static value. This is the dispatcher's
    single injection point for the controller.
    """
    merged = dict(static)
    for queue_key, limit in advisory.items():
        if queue_key in merged:
            merged[queue_key] = limit
    return merged
