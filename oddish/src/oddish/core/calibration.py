"""Offline, read-only per-queue concurrency calibration over historic trials.

This is a measurement tool, not a control loop: it aggregates the ``trials``
table by ``queue_key`` over a LONG window into the inputs the dynamic-concurrency
controller needs to derive a provider-limit ceiling and to back-test the
limit->ceiling mapping before any live change --

* ``throughput_per_min`` -- realized finished-trial throughput,
* ``avg_trial_seconds`` -- mean trial residence time (Little's-Law factor),
* ``r_tokens`` -- mean tokens per trial (the TPM-budget denominator),
* ``throttle_rate`` -- fraction of trials whose error looks like a provider
  rate limit, reusing the existing ``classify_retry_reason`` matcher, and
* ``token_coverage`` -- fraction of trials that recorded token usage, so a
  low-coverage queue's TPM mapping can be distrusted in favor of RPM.

It makes NO schema change and NO dispatch mutation; it only reads. Run it as
``python -m oddish.core.calibration [--window-minutes N]`` to print the JSON
report (per the project convention, point it at the preview DB first).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import utcnow
from oddish.db.models import TrialModel

# A long default window (7 days) chosen well above the controller's 240s
# feedback cadence, so the calibration is not derived from metrics already
# shaped by the live AIMD loop.
DEFAULT_CALIBRATION_WINDOW_MINUTES = 7 * 24 * 60


@dataclass(frozen=True)
class QueueCalibration:
    """Per-``queue_key`` historic calibration over the analysis window."""

    queue_key: str
    provider: str
    trials: int
    token_coverage: float
    avg_trial_seconds: float | None
    r_tokens: float | None
    throughput_per_min: float
    throttle_rate: float


def count_rate_limit_by_queue(
    rows: list[tuple[str, str | None]],
) -> dict[str, int]:
    """Count provider-throttle errors per ``queue_key``.

    Reuses the existing ``classify_retry_reason`` rate-limit matcher (rather than
    re-implementing the regex or porting it to a Postgres ``~*``), so the offline
    throttle rate is computed exactly as the live retry scheduler classifies it.
    ``rows`` are ``(queue_key, error_message)`` pairs.
    """
    # Imported lazily so this read-only core module does not pull the worker
    # job chain into a server-only install (mirrors core.admin's lazy imports).
    from oddish.workers.queue.worker_job_single_job import classify_retry_reason

    counts: dict[str, int] = {}
    for queue_key, error_message in rows:
        if classify_retry_reason(error_message) == "rate_limit":
            counts[queue_key] = counts.get(queue_key, 0) + 1
    return counts


def summarize_calibration(
    agg_rows: list[dict],
    throttle_counts: dict[str, int],
    *,
    window_minutes: float,
) -> list[QueueCalibration]:
    """Combine the per-queue aggregate rows and throttle counts into metrics.

    Pure: ``agg_rows`` carry the SQL aggregates (``trials``, ``token_covered``,
    ``finished``, ``avg_trial_seconds``, ``r_tokens``); ``throttle_counts`` map
    ``queue_key`` to its rate-limit-looking error count. Queues with no trials
    are dropped. ``throughput_per_min`` is finished trials over the window.
    """
    out: list[QueueCalibration] = []
    for row in agg_rows:
        trials = int(row["trials"])
        if trials <= 0:
            continue
        finished = int(row["finished"])
        avg_seconds = row["avg_trial_seconds"]
        r_tokens = row["r_tokens"]
        out.append(
            QueueCalibration(
                queue_key=row["queue_key"],
                provider=row["provider"],
                trials=trials,
                token_coverage=int(row["token_covered"]) / trials,
                avg_trial_seconds=(
                    float(avg_seconds) if avg_seconds is not None else None
                ),
                r_tokens=(float(r_tokens) if r_tokens is not None else None),
                throughput_per_min=(
                    finished / window_minutes if window_minutes > 0 else 0.0
                ),
                throttle_rate=throttle_counts.get(row["queue_key"], 0) / trials,
            )
        )
    return out


async def calibrate_model_concurrency(
    session: AsyncSession,
    *,
    window_minutes: float = DEFAULT_CALIBRATION_WINDOW_MINUTES,
) -> list[QueueCalibration]:
    """Read ``trials`` over the window and return per-queue calibration metrics.

    Read-only. Mirrors ``core.dashboard.get_model_usage_core`` but groups by
    ``queue_key`` and adds token coverage + the reused throttle classifier. The
    throttle rate runs the Python matcher over fetched error messages (only
    errored trials carry one), avoiding a ``\\b``->``\\y`` Postgres regex port.
    """
    since = utcnow() - timedelta(minutes=window_minutes)

    agg_query = (
        select(
            TrialModel.queue_key,
            TrialModel.provider,
            func.count(TrialModel.id).label("trials"),
            func.count(case((TrialModel.input_tokens.isnot(None), 1))).label(
                "token_covered"
            ),
            func.count(case((TrialModel.finished_at.isnot(None), 1))).label("finished"),
            func.avg(
                case(
                    (
                        TrialModel.finished_at.isnot(None),
                        func.extract(
                            "epoch", TrialModel.finished_at - TrialModel.started_at
                        ),
                    )
                )
            ).label("avg_trial_seconds"),
            func.avg(
                func.coalesce(TrialModel.input_tokens, 0)
                + func.coalesce(TrialModel.cache_tokens, 0)
                + func.coalesce(TrialModel.output_tokens, 0)
            ).label("r_tokens"),
        )
        .where(TrialModel.created_at >= since)
        .group_by(TrialModel.queue_key, TrialModel.provider)
    )
    agg_result = (await session.execute(agg_query)).all()
    agg_rows = [
        {
            "queue_key": row.queue_key,
            "provider": row.provider,
            "trials": row.trials,
            "token_covered": row.token_covered,
            "finished": row.finished,
            "avg_trial_seconds": row.avg_trial_seconds,
            "r_tokens": row.r_tokens,
        }
        for row in agg_result
    ]

    throttle_query = select(TrialModel.queue_key, TrialModel.error_message).where(
        TrialModel.created_at >= since,
        TrialModel.error_message.isnot(None),
    )
    throttle_result = (await session.execute(throttle_query)).all()
    throttle_counts = count_rate_limit_by_queue(
        [(row.queue_key, row.error_message) for row in throttle_result]
    )

    return summarize_calibration(
        agg_rows, throttle_counts, window_minutes=window_minutes
    )


def format_calibration_json(rows: list[QueueCalibration]) -> str:
    """Render the calibration as a stable, pretty-printed JSON array."""
    import json

    return json.dumps([asdict(row) for row in rows], indent=2, default=str)


async def _load_calibration(window_minutes: float) -> list[QueueCalibration]:
    """Open a DB session and run the calibration (the only DB-touching seam)."""
    from oddish.db import get_session

    async with get_session() as session:
        return await calibrate_model_concurrency(session, window_minutes=window_minutes)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: print the per-queue calibration as JSON."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="oddish-calibrate",
        description="Read-only per-queue concurrency calibration over trials.",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=DEFAULT_CALIBRATION_WINDOW_MINUTES,
        help="Analysis window in minutes (default: 7 days).",
    )
    args = parser.parse_args(argv)
    rows = asyncio.run(_load_calibration(args.window_minutes))
    print(format_calibration_json(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI shim
    raise SystemExit(main())
