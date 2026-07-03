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
