from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.models.job.result import JobResult

from oddish.core.harbor_artifacts import (
    build_trial_result,
    detect_trajectory,
    extract_ctrf_summary,
    extract_reward_details_summary,
    extract_trajectory_metrics,
    extract_trial_result_fields,
    extract_verifier_metrics,
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
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    trajectory_duration_seconds: float | None = None
    total_tool_calls: int | None = None
    tool_counts: dict[str, int] | None = None
    cost_usd: float | None = None

    # Per-phase timing breakdown (seconds)
    phase_timing: dict[str, Any] | None = None

    # Whether an ATIF trajectory file exists
    has_trajectory: bool = False

    # Structured benchmark metrics the task's verifier reported via
    # ``verifier/metrics.json`` (persisted to ``trials.result``).
    metrics: dict[str, Any] | None = None

    # Compact Common Test Report Format summary from ``verifier/ctrf.json``.
    # The full report remains in S3; only counts are persisted on the row.
    verifier_summary: dict[str, Any] | None = None

    # Every named reward the verifier reported (``verifier_result.rewards``,
    # i.e. RewardKit dimensions plus reward.toml aggregates). ``reward`` above
    # stays the headline scalar this map collapses to.
    rewards: dict[str, float] | None = None

    # Compact per-criterion breakdown from ``verifier/reward-details.json``.
    # The full document (complete judge reasoning) remains in S3.
    reward_details: dict[str, Any] | None = None

    # The Python exception class name (e.g. "AddTestsDirError",
    # "AgentTimeoutError") that ended this trial, sourced from
    # ``TrialResult.exception_info.exception_type`` when Harbor produced one,
    # or ``type(exc).__name__`` when ``run_harbor_trial_async`` itself caught
    # an exception. Used by ``trial_handler._store_trial_results`` to skip
    # trial-level retries on outcomes Harbor's own RetryConfig already marks
    # as non-retryable.
    exception_type: str | None = None


def merged_trial_result(
    metrics: dict[str, Any] | None,
    error: str | None,
    exception_type: str | None,
    verifier_summary: dict[str, Any] | None = None,
    rewards: dict[str, float] | None = None,
    reward_details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Trial ``result`` payload: verifier metrics plus a quiet-exception marker.

    A trial can complete (verifier ran, reward recorded) even though a phase
    raised -- e.g. the agent exited non-zero on an invalid model id. Status
    alone then reads as an ordinary reward-0 eval. When Harbor recorded an
    exception, merge a ``harbor_exception`` marker into the result payload so
    hard failures are distinguishable from legitimate zero-reward runs without
    parsing error strings. The key is reserved: a task metric of the same name
    is overwritten.
    """
    return build_trial_result(
        metrics,
        verifier_summary,
        error,
        exception_type,
        rewards=rewards,
        reward_details=reward_details,
    )


def _detect_trajectory(job_dir: Path) -> bool:
    """Backward-compatible wrapper for tests/imports."""
    return detect_trajectory(job_dir)


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
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None
    phase_timing: dict[str, Any] | None = None
    trial_reward: float | None = None
    rewards_map: dict[str, float] | None = None

    for trial_result in job_result.trial_results:
        fields = extract_trial_result_fields(trial_result)
        if error is None and fields.error is not None:
            error = fields.error
            exception_type = fields.exception_type
        if trial_reward is None and fields.reward is not None:
            trial_reward = fields.reward
        if rewards_map is None and fields.rewards is not None:
            rewards_map = fields.rewards
        if input_tokens is None and output_tokens is None:
            input_tokens = fields.input_tokens
            cache_tokens = fields.cache_tokens
            output_tokens = fields.output_tokens
            cost_usd = fields.cost_usd
        if phase_timing is None and fields.phase_timing is not None:
            phase_timing = fields.phase_timing
        if (
            (error is not None or exception_type is not None)
            and trial_reward is not None
            and (input_tokens is not None or output_tokens is not None)
            and phase_timing is not None
        ):
            break

    trajectory = extract_trajectory_metrics(job_dir)
    if input_tokens is None and output_tokens is None:
        input_tokens = trajectory.input_tokens
        output_tokens = trajectory.output_tokens
        cache_tokens = trajectory.cache_tokens
    cache_write_tokens = trajectory.cache_write_tokens
    total_steps = trajectory.total_steps
    if cost_usd is None:
        cost_usd = trajectory.cost_usd

    has_trajectory = detect_trajectory(job_dir)
    metrics = extract_verifier_metrics(job_dir)
    verifier_summary = extract_ctrf_summary(job_dir)
    reward_details = extract_reward_details_summary(job_dir)

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
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            total_steps=total_steps,
            trajectory_duration_seconds=trajectory.trajectory_duration_seconds,
            total_tool_calls=trajectory.total_tool_calls,
            tool_counts=trajectory.tool_counts,
            cost_usd=cost_usd,
            phase_timing=phase_timing,
            has_trajectory=has_trajectory,
            metrics=metrics,
            verifier_summary=verifier_summary,
            rewards=rewards_map,
            reward_details=reward_details,
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

    return _outcome(trial_reward)
