from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.models.job.result import JobResult

from oddish.core.harbor_artifacts import (
    detect_trajectory,
    extract_trajectory_metrics,
    extract_trial_result_fields,
)


@dataclass(frozen=True)
class HarborOutcome:
    """Oddish-specific summary of a Harbor trial execution.

    Not Harbor's TrialResult/JobResult -- this flattens the deeply nested Harbor
    result tree into a simple struct that Oddish persists to Postgres and returns
    via its API. Fields like reward, cost_usd, and phase_timing are extracted
    from Harbor's TrialResult/AgentContext/VerifierResult in
    _extract_outcome_from_job_result().
    """

    reward: float | None
    error: str | None
    exit_code: int
    duration_sec: float
    job_result_path: Path | None
    job_dir: Path | None  # Full job directory for S3 upload

    # Token usage, steps & cost (from Harbor's AgentContext / ATIF final_metrics)
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None

    # Per-phase timing breakdown (seconds)
    phase_timing: dict[str, Any] | None = None

    # Whether an ATIF trajectory file exists
    has_trajectory: bool = False

    # The Python exception class name (e.g. "AddTestsDirError",
    # "AgentTimeoutError") that ended this trial, sourced from
    # ``TrialResult.exception_info.exception_type`` when Harbor produced one,
    # or ``type(exc).__name__`` when ``run_harbor_trial_async`` itself caught
    # an exception. Used by ``trial_handler._store_trial_results`` to skip
    # trial-level retries on outcomes Harbor's own RetryConfig already marks
    # as non-retryable.
    exception_type: str | None = None


def _detect_trajectory(job_dir: Path) -> bool:
    """Backward-compatible wrapper for tests/imports."""
    return detect_trajectory(job_dir)


def _extract_metrics_from_trajectory(
    job_dir: Path,
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    """Backward-compatible wrapper for tests/imports."""
    metrics = extract_trajectory_metrics(job_dir)
    return (
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.cache_tokens,
        metrics.total_steps,
        metrics.cost_usd,
    )


def _extract_outcome_from_job_result(
    job_result: JobResult,
    job_result_path: Path,
    job_dir: Path,
    duration_sec: float,
) -> HarborOutcome:
    """Extract reward, error, token usage, timing, and trajectory from Harbor's JobResult."""
    error: str | None = None
    exception_type: str | None = None
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None
    phase_timing: dict[str, Any] | None = None

    for trial_result in job_result.trial_results:
        fields = extract_trial_result_fields(trial_result, artifact_dir=job_dir)
        if error is None and fields.error is not None:
            error = fields.error
            exception_type = fields.exception_type
        if input_tokens is None and output_tokens is None:
            input_tokens = fields.input_tokens
            cache_tokens = fields.cache_tokens
            output_tokens = fields.output_tokens
            total_steps = fields.total_steps
            cost_usd = fields.cost_usd
        if phase_timing is None and fields.phase_timing is not None:
            phase_timing = fields.phase_timing
        if (
            (error is not None or exception_type is not None)
            and (input_tokens is not None or output_tokens is not None)
            and phase_timing is not None
        ):
            break

    has_trajectory = detect_trajectory(job_dir)

    def _outcome(reward: float | None) -> HarborOutcome:
        return HarborOutcome(
            reward=reward,
            error=error,
            exit_code=0,
            duration_sec=duration_sec,
            job_result_path=job_result_path,
            job_dir=job_dir,
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            total_steps=total_steps,
            cost_usd=cost_usd,
            phase_timing=phase_timing,
            has_trajectory=has_trajectory,
            exception_type=exception_type,
        )

    # Harbor's AgentDatasetStats.reward_stats is
    # ``dict[str, dict[float | int, list[str]]]`` where the innermost value
    # is the list of trial IDs that produced each reward value. Pick the
    # reward with the most trial IDs (most frequent outcome).
    if job_result.stats.evals:
        first_eval = next(iter(job_result.stats.evals.values()))
        if first_eval.reward_stats and "reward" in first_eval.reward_stats:
            reward_map = first_eval.reward_stats["reward"]
            for reward_key, trial_ids in sorted(
                reward_map.items(),
                key=lambda item: len(item[1]),
                reverse=True,
            ):
                if not trial_ids:
                    continue
                try:
                    return _outcome(float(reward_key))
                except (TypeError, ValueError):
                    continue

    for trial_result in job_result.trial_results:
        if trial_result.verifier_result and trial_result.verifier_result.rewards:
            reward_value = trial_result.verifier_result.rewards.get("reward")
            if reward_value is not None:
                return _outcome(float(reward_value))

    return _outcome(None)
