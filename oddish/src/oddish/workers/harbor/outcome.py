from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.models.job.result import JobResult


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

    # Full sub-reward breakdown from the verifier, when the grader emits one
    # (partial-credit components: code_fraction, code_tests_passed/total,
    # workflow_fraction, workflow_passed/total, ...). None for graders that
    # only produce a scalar reward. Persisted to ``trial.result`` so the score
    # is explainable in the UI/CLI instead of being a bare number.
    reward_breakdown: dict[str, Any] | None = None


def _extract_timing_info(trial_result: Any) -> dict[str, Any] | None:
    """Extract per-phase timing from a TrialResult's TimingInfo fields."""
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


def _detect_trajectory(job_dir: Path) -> bool:
    """Check if any ATIF trajectory file exists in the job output."""
    if not job_dir or not job_dir.exists():
        return False
    if any(job_dir.rglob("trajectory.json")):
        return True
    if any(job_dir.rglob("trajectory.jsonl")):
        return True
    return False


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_metrics_from_trajectory(
    job_dir: Path,
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    """Fallback: read token/step/cost metrics from ATIF trajectory data."""
    if not job_dir or not job_dir.exists():
        return None, None, None, None, None
    for traj_path in job_dir.rglob("trajectory.json"):
        try:
            data = json.loads(traj_path.read_text())
            fm = data.get("final_metrics")
            steps = data.get("steps")
            if not fm and not isinstance(steps, list):
                continue
            total_steps = _as_int(fm.get("total_steps")) if isinstance(fm, dict) else None
            if total_steps is None and isinstance(steps, list):
                total_steps = len(steps)
            return (
                fm.get("total_prompt_tokens") if isinstance(fm, dict) else None,
                fm.get("total_completion_tokens") if isinstance(fm, dict) else None,
                fm.get("total_cached_tokens") if isinstance(fm, dict) else None,
                total_steps,
                fm.get("total_cost_usd") if isinstance(fm, dict) else None,
            )
        except Exception:
            continue
    return None, None, None, None, None


def _extract_outcome_from_job_result(
    job_result: JobResult,
    job_result_path: Path,
    job_dir: Path,
    duration_sec: float,
) -> HarborOutcome:
    """Extract reward, error, token usage, timing, and trajectory from Harbor's JobResult."""
    error: str | None = None
    exception_type: str | None = None
    for trial_result in job_result.trial_results:
        if trial_result.exception_info:
            exc = trial_result.exception_info
            msg = exc.exception_message or exc.exception_type
            if msg:
                error = str(msg)
            if exc.exception_type:
                exception_type = str(exc.exception_type)
            if error or exception_type:
                break

    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    total_steps: int | None = None
    cost_usd: float | None = None
    phase_timing: dict[str, Any] | None = None

    for trial_result in job_result.trial_results:
        ctx = trial_result.agent_result
        if ctx and not ctx.is_empty():
            input_tokens = ctx.n_input_tokens
            cache_tokens = ctx.n_cache_tokens
            output_tokens = ctx.n_output_tokens
            cost_usd = ctx.cost_usd
            break

    if input_tokens is None and output_tokens is None:
        t_in, t_out, t_cache, t_steps, t_cost = _extract_metrics_from_trajectory(job_dir)
        input_tokens = t_in
        output_tokens = t_out
        cache_tokens = t_cache
        total_steps = t_steps
        if cost_usd is None:
            cost_usd = t_cost
    else:
        _, _, _, total_steps, t_cost = _extract_metrics_from_trajectory(job_dir)
        if cost_usd is None:
            cost_usd = t_cost

    for trial_result in job_result.trial_results:
        phase_timing = _extract_timing_info(trial_result)
        if phase_timing:
            break

    has_trajectory = _detect_trajectory(job_dir)

    # Capture the verifier's full sub-reward dict once (code/workflow fractions,
    # tests passed/total, ...) so it rides along with whichever reward we return.
    reward_breakdown: dict[str, Any] | None = None
    for trial_result in job_result.trial_results:
        vr = trial_result.verifier_result
        if vr and vr.rewards:
            reward_breakdown = dict(vr.rewards)
            break

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
            reward_breakdown=reward_breakdown,
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
