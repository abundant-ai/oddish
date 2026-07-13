from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class HarborTrajectoryMetrics:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class HarborTrialExtraction:
    reward: float | None
    error: str | None
    exception_type: str | None
    input_tokens: int | None
    cache_tokens: int | None
    output_tokens: int | None
    total_steps: int | None
    cost_usd: float | None
    phase_timing: dict[str, Any] | None


def detect_trajectory(path: Path) -> bool:
    """Return True when a Harbor output tree contains an ATIF trajectory."""
    if not path or not path.exists():
        return False
    return any(path.rglob("trajectory.json")) or any(path.rglob("trajectory.jsonl"))


# Benchmark tasks report structured metrics by writing this file from their
# verifier (next to reward.txt). Kept small: the payload lands in a JSONB
# column and rides every trial-detail response.
VERIFIER_METRICS_MAX_BYTES = 64 * 1024

# CTRF reports can include every test name, failure message, and stack trace,
# so they are commonly much larger than metrics.json. We only persist the
# compact summary below, but still bound the source document before parsing it
# in a worker process.
VERIFIER_CTRF_MAX_BYTES = 8 * 1024 * 1024


def _reject_nonfinite(name: str):
    raise ValueError(f"non-finite JSON constant in metrics: {name}")


def extract_verifier_metrics(path: Path) -> dict[str, Any] | None:
    """The first ``verifier/metrics.json`` under a Harbor output tree, or None.

    Forgiving by design -- a missing, oversized, malformed, or non-object file
    yields None rather than an error, so a broken metrics emission can never
    take down a trial whose reward already settled.
    """
    if not path or not path.exists():
        return None
    for metrics_path in sorted(path.rglob("verifier/metrics.json")):
        try:
            if metrics_path.stat().st_size > VERIFIER_METRICS_MAX_BYTES:
                continue
            # Reject NaN/Infinity: json.loads accepts them, but they do not
            # survive JSONB persistence (trials.result), which would fail an
            # otherwise-complete trial at finalization.
            payload = json.loads(
                metrics_path.read_text(),
                parse_constant=_reject_nonfinite,
            )
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
    return None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def extract_ctrf_summary(path: Path) -> dict[str, Any] | None:
    """Return a compact summary from the first valid ``verifier/ctrf.json``.

    Harbor tasks using pytest-json-ctrf (and any other CTRF reporter) write a
    standard report next to ``reward.txt``. The full report stays in object
    storage; only aggregate counts and the reporter name ride in
    ``trials.result`` so the trial drawer can render them without an S3 read.

    Missing, oversized, malformed, or structurally invalid candidates are
    ignored, just like metrics.json. A report problem must never change the
    settled verifier reward.
    """
    if not path or not path.exists():
        return None
    for report_path in sorted(path.rglob("verifier/ctrf.json")):
        try:
            if report_path.stat().st_size > VERIFIER_CTRF_MAX_BYTES:
                continue
            payload = json.loads(
                report_path.read_text(),
                parse_constant=_reject_nonfinite,
            )
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, dict):
            continue
        summary = results.get("summary")
        if not isinstance(summary, dict):
            continue

        counts: dict[str, int] = {}
        for key in ("tests", "passed", "failed", "skipped", "pending", "other"):
            value = _nonnegative_int(summary.get(key))
            if value is None:
                break
            counts[key] = value
        else:
            compact: dict[str, Any] = {"format": "ctrf", **counts}
            tool = results.get("tool")
            if isinstance(tool, dict):
                tool_name = tool.get("name")
                if isinstance(tool_name, str) and tool_name.strip():
                    compact["tool"] = tool_name.strip()[:80]
            return compact
    return None


def build_trial_result(
    metrics: dict[str, Any] | None,
    verifier_summary: dict[str, Any] | None,
    error: str | None,
    exception_type: str | None,
) -> dict[str, Any] | None:
    """Merge verifier metrics, a compact report, and a quiet exception marker."""
    result: dict[str, Any] = dict(metrics or {})
    if verifier_summary is not None:
        # Reserved underscore key: task-authored metrics may use arbitrary
        # names, while Oddish owns this normalized verifier-report envelope.
        result["_verifier"] = verifier_summary
    if exception_type is not None:
        result["harbor_exception"] = {
            "exception_type": exception_type,
            "error": error[:300] if error else None,
        }
    return result or None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens",
    "input_cache_creation",
    "cacheWriteTokens",
    "cache_write_tokens",
)


def _sum_cache_write_from_steps(steps: list) -> int | None:
    total = 0
    found = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        metrics = step.get("metrics") or {}
        extra = metrics.get("extra") or {} if isinstance(metrics, dict) else {}
        if not isinstance(extra, dict):
            continue
        for key in _CACHE_WRITE_KEYS:
            val = extra.get(key)
            if val is not None:
                n = _as_int(val)
                if n is not None:
                    total += n
                    found = True
                break
    return total if found else None


def _cache_write_from_final_metrics(fm: dict) -> int | None:
    extra = fm.get("extra")
    if not isinstance(extra, dict):
        return None
    for key in _CACHE_WRITE_KEYS:
        val = extra.get(key)
        if val is not None:
            return _as_int(val)
    return None


def cache_write_tokens_from_trajectory(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    steps = data.get("steps")
    total = _sum_cache_write_from_steps(steps) if isinstance(steps, list) else None
    if total is not None:
        return total
    final_metrics = data.get("final_metrics")
    if isinstance(final_metrics, dict):
        return _cache_write_from_final_metrics(final_metrics)
    return None


def extract_trajectory_metrics(path: Path) -> HarborTrajectoryMetrics:
    """Read token, step, and cost metrics from ATIF trajectory data."""
    if not path or not path.exists():
        return HarborTrajectoryMetrics()

    for traj_path in path.rglob("trajectory.json"):
        try:
            data = json.loads(traj_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        final_metrics = data.get("final_metrics")
        steps = data.get("steps")
        if not isinstance(final_metrics, dict) and not isinstance(steps, list):
            continue

        total_steps = (
            _as_int(final_metrics.get("total_steps"))
            if isinstance(final_metrics, dict)
            else None
        )
        if total_steps is None and isinstance(steps, list):
            total_steps = len(steps)

        cache_write_tokens: int | None = None
        if isinstance(steps, list):
            cache_write_tokens = _sum_cache_write_from_steps(steps)
        if cache_write_tokens is None and isinstance(final_metrics, dict):
            cache_write_tokens = _cache_write_from_final_metrics(final_metrics)

        return HarborTrajectoryMetrics(
            input_tokens=(
                _as_int(final_metrics.get("total_prompt_tokens"))
                if isinstance(final_metrics, dict)
                else None
            ),
            output_tokens=(
                _as_int(final_metrics.get("total_completion_tokens"))
                if isinstance(final_metrics, dict)
                else None
            ),
            cache_tokens=(
                _as_int(final_metrics.get("total_cached_tokens"))
                if isinstance(final_metrics, dict)
                else None
            ),
            cache_write_tokens=cache_write_tokens,
            total_steps=total_steps,
            cost_usd=(
                _as_float(final_metrics.get("total_cost_usd"))
                if isinstance(final_metrics, dict)
                else None
            ),
        )

    return HarborTrajectoryMetrics()


def extract_timing_info(trial_result: Any) -> dict[str, Any] | None:
    """Extract per-phase timing from a Harbor TrialResult-like object."""
    timing: dict[str, Any] = {}
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        info = getattr(trial_result, phase, None)
        if info and info.started_at and info.finished_at:
            timing[phase] = {
                "started_at": info.started_at.isoformat(),
                "finished_at": info.finished_at.isoformat(),
                "duration_sec": round(
                    (info.finished_at - info.started_at).total_seconds(), 2
                ),
            }
    return timing or None


def _extract_reward(trial_result: Any) -> float | None:
    verifier_result = getattr(trial_result, "verifier_result", None)
    rewards = getattr(verifier_result, "rewards", None)
    if not rewards:
        return None
    reward_value = rewards.get("reward")
    if reward_value is None and len(rewards) == 1:
        reward_value = next(iter(rewards.values()))
    if reward_value is None:
        return None
    return _as_float(reward_value)


def _extract_error(trial_result: Any) -> tuple[str | None, str | None]:
    exc = getattr(trial_result, "exception_info", None)
    if exc is None:
        return None, None
    exception_type = getattr(exc, "exception_type", None)
    message = (
        getattr(exc, "exception_message", None)
        or exception_type
        or "Harbor execution error"
    )
    return str(message) if message else None, (
        str(exception_type) if exception_type else None
    )


def _extract_token_cost_totals(
    trial_result: Any,
) -> tuple[int | None, int | None, int | None, float | None]:
    compute_totals = getattr(trial_result, "compute_token_cost_totals", None)
    if callable(compute_totals):
        return cast(
            tuple[int | None, int | None, int | None, float | None],
            compute_totals(),
        )

    context = getattr(trial_result, "agent_result", None)
    is_empty = getattr(context, "is_empty", None)
    if context is None or (callable(is_empty) and is_empty()):
        return None, None, None, None
    return (
        context.n_input_tokens,
        context.n_cache_tokens,
        context.n_output_tokens,
        context.cost_usd,
    )


def extract_trial_result_fields(
    trial_result: Any,
    *,
    artifact_dir: Path | None = None,
) -> HarborTrialExtraction:
    """Flatten a Harbor TrialResult-like object into Oddish persistence fields."""
    error, exception_type = _extract_error(trial_result)
    input_tokens, cache_tokens, output_tokens, cost_usd = _extract_token_cost_totals(
        trial_result
    )
    total_steps: int | None = None

    if artifact_dir is not None:
        trajectory = extract_trajectory_metrics(artifact_dir)
        if input_tokens is None and output_tokens is None:
            input_tokens = trajectory.input_tokens
            output_tokens = trajectory.output_tokens
            cache_tokens = trajectory.cache_tokens
        total_steps = trajectory.total_steps
        if cost_usd is None:
            cost_usd = trajectory.cost_usd

    return HarborTrialExtraction(
        reward=_extract_reward(trial_result),
        error=error,
        exception_type=exception_type,
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        output_tokens=output_tokens,
        total_steps=total_steps,
        cost_usd=cost_usd,
        phase_timing=extract_timing_info(trial_result),
    )
