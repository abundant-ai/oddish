from __future__ import annotations

import json
import math
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
    trajectory_duration_seconds: float | None = None
    total_tool_calls: int | None = None
    tool_counts: dict[str, int] | None = None
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
    rewards: dict[str, float] | None = None


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


def sanitize_task_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copy task-authored result data without Oddish-owned verifier fields."""
    sanitized = dict(result or {})
    sanitized.pop("_verifier", None)
    sanitized.pop("_rewards", None)
    sanitized.pop("_reward_details", None)
    return sanitized or None


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
            compact: dict[str, Any] = {
                "format": "ctrf",
                **counts,
                "report_path": report_path.relative_to(path).as_posix(),
            }
            tool = results.get("tool")
            if isinstance(tool, dict):
                tool_name = tool.get("name")
                if isinstance(tool_name, str) and tool_name.strip():
                    compact["tool"] = tool_name.strip()[:80]
            return compact
    return None


# RewardKit-style verifiers (harbor-rewardkit) report named reward dimensions
# and aggregates in ``verifier/reward.json`` and write the per-criterion
# breakdown (weights, judge reasoning, errors) to
# ``verifier/reward-details.json`` beside it. Judge reasoning makes the details
# file arbitrarily large, so like CTRF the source document is bounded before
# parsing and only a compact summary rides in ``trials.result``; the full file
# stays in object storage for the drawer to lazy-load.
VERIFIER_REWARD_DETAILS_MAX_BYTES = 8 * 1024 * 1024
REWARD_DETAILS_EMBED_MAX_BYTES = 48 * 1024
REWARD_DETAILS_RAW_MAX_CHARS = 120
REWARD_DETAILS_MAX_JUDGE_FILES = 8
REWARDS_MAP_MAX_KEYS = 32


def _finite_rounded(value: Any) -> float | None:
    number = _as_float(value)
    if number is None or not math.isfinite(number):
        return None
    return round(number, 4)


class _ClipTracker:
    """Marks a summary as lossy so clients know to fetch the full document."""

    def __init__(self) -> None:
        self.clipped = False

    def mark(self) -> None:
        self.clipped = True


def _compact_reward_criterion(
    raw: Any, reasoning_limit: int, text_limit: int, clip: _ClipTracker
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    value = _finite_rounded(raw.get("value"))
    if not isinstance(name, str) or not name or value is None:
        return None
    compact: dict[str, Any] = {"name": name[:64], "value": value}
    weight = _finite_rounded(raw.get("weight"))
    if weight is not None:
        compact["weight"] = weight
    # The pre-normalization answer ("yes", 4, true) is what the drawer shows
    # as "judge answered". Only scalar values ride; anything else stays in
    # the full document.
    answer = raw.get("raw")
    if isinstance(answer, bool) or (
        isinstance(answer, (int, float)) and math.isfinite(answer)
    ):
        compact["raw"] = answer
    elif isinstance(answer, str) and answer:
        if len(answer) > REWARD_DETAILS_RAW_MAX_CHARS:
            clip.mark()
        compact["raw"] = answer[:REWARD_DETAILS_RAW_MAX_CHARS]
    elif answer is not None:
        clip.mark()
    for key, limit in (
        ("description", text_limit),
        ("reasoning", reasoning_limit),
        ("error", text_limit),
    ):
        text = raw.get(key)
        if isinstance(text, str) and text:
            if len(text) > limit:
                clip.mark()
            if limit > 0:
                compact[key] = text[:limit]
    for flag in ("negate", "optional"):
        if raw.get(flag) is True:
            compact[flag] = True
    return compact


def _compact_reward_dimension(
    name: str,
    raw: Any,
    reasoning_limit: int,
    text_limit: int,
    max_criteria: int,
    clip: _ClipTracker,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    score = _finite_rounded(raw.get("score"))
    if score is None:
        return None
    compact: dict[str, Any] = {"name": name[:64], "score": score}
    kind = raw.get("kind")
    if isinstance(kind, str) and kind:
        compact["kind"] = kind[:24]
    judge = raw.get("judge")
    if isinstance(judge, dict):
        judge_model = judge.get("model") or judge.get("agent")
        if isinstance(judge_model, str) and judge_model:
            compact["judge"] = judge_model[:120]
        # The exact inputs the judge read — the drawer's "Judge read" chips.
        files = judge.get("files")
        if isinstance(files, (list, tuple)):
            judge_files = [
                f[:200]
                for f in files[:REWARD_DETAILS_MAX_JUDGE_FILES]
                if isinstance(f, str) and f
            ]
            if judge_files:
                compact["judge_files"] = judge_files
        trajectory = judge.get("atif_trajectory")
        if isinstance(trajectory, str) and trajectory:
            compact["judge_trajectory"] = trajectory[:200]
    criteria: list[dict[str, Any]] = []
    criteria_raw = raw.get("criteria")
    total = 0
    if isinstance(criteria_raw, list):
        total = len(criteria_raw)
        if total > max_criteria:
            clip.mark()
        for entry in criteria_raw[:max_criteria]:
            criterion = _compact_reward_criterion(
                entry, reasoning_limit, text_limit, clip
            )
            if criterion is None:
                # A single malformed criterion must not cost the whole
                # breakdown; drop it, keep its siblings, and mark the summary
                # lossy so the drawer fetches the full document.
                clip.mark()
                continue
            criteria.append(criterion)
    compact["criteria"] = criteria
    if total > len(criteria):
        compact["criteria_total"] = total
    warnings = raw.get("warnings")
    if isinstance(warnings, list) and warnings:
        compact["warnings"] = len(warnings)
    return compact


def extract_reward_details_summary(path: Path) -> dict[str, Any] | None:
    """Compact summary of the first valid ``verifier/reward-details.json``.

    The summary keeps every reward dimension (score, kind, judge model) and its
    criteria (value, weight, truncated description/reasoning/error) inside a
    bounded payload: progressively tighter truncation tiers are tried until the
    encoded summary fits ``REWARD_DETAILS_EMBED_MAX_BYTES``. ``details_path``
    points back at the full document in object storage so clients can
    lazy-load complete judge reasoning, mirroring the CTRF ``report_path``
    pattern.

    Missing, oversized, malformed, or structurally invalid candidates are
    ignored, just like metrics.json -- a details problem must never change the
    settled verifier reward.
    """
    if not path or not path.exists():
        return None
    for details_path in sorted(path.rglob("verifier/reward-details.json")):
        try:
            if details_path.stat().st_size > VERIFIER_REWARD_DETAILS_MAX_BYTES:
                continue
            payload = json.loads(
                details_path.read_text(),
                parse_constant=_reject_nonfinite,
            )
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload:
            continue
        rel_path = details_path.relative_to(path).as_posix()
        # (reasoning limit, description/error limit, criteria cap) tiers, from
        # generous to score-only. The first tier whose encoding fits the embed
        # budget wins. ``truncated`` marks any loss against the source
        # document — a tighter tier, clipped text, capped criteria lists, or
        # a dropped malformed criterion — so clients know to fetch the full
        # report rather than trust the embed as complete.
        for reasoning_limit, text_limit, max_criteria in (
            (400, 300, 40),
            (120, 120, 20),
            (0, 0, 12),
            (0, 0, 0),
        ):
            clip = _ClipTracker()
            dimensions: list[dict[str, Any]] = []
            valid = True
            for name, raw in payload.items():
                if not isinstance(name, str) or not name:
                    valid = False
                    break
                entries = raw if isinstance(raw, list) else [raw]
                for entry in entries:
                    compact = _compact_reward_dimension(
                        name, entry, reasoning_limit, text_limit, max_criteria, clip
                    )
                    if compact is None:
                        valid = False
                        break
                    dimensions.append(compact)
                if not valid:
                    break
            if not valid or not dimensions:
                break
            summary: dict[str, Any] = {
                "format": "rewardkit",
                "details_path": rel_path,
                "dimensions": dimensions,
            }
            if clip.clipped:
                summary["truncated"] = True
            encoded = json.dumps(summary, separators=(",", ":"))
            if len(encoded.encode("utf-8")) <= REWARD_DETAILS_EMBED_MAX_BYTES:
                return summary
    return None


def build_trial_result(
    metrics: dict[str, Any] | None,
    verifier_summary: dict[str, Any] | None,
    error: str | None,
    exception_type: str | None,
    rewards: dict[str, float] | None = None,
    reward_details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge verifier metrics, a compact report, and a quiet exception marker."""
    result: dict[str, Any] = sanitize_task_result(metrics) or {}
    if verifier_summary is not None:
        result["_verifier"] = verifier_summary
    if rewards is not None:
        result["_rewards"] = rewards
    if reward_details is not None:
        result["_reward_details"] = reward_details
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

        tool_counts: dict[str, int] = {}
        timestamps: list[float] = []
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                raw_timestamp = step.get("timestamp")
                if isinstance(raw_timestamp, str):
                    try:
                        from datetime import datetime

                        timestamps.append(
                            datetime.fromisoformat(
                                raw_timestamp.replace("Z", "+00:00")
                            ).timestamp()
                        )
                    except ValueError:
                        pass
                for call in step.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("function_name") or call.get("name")
                    if name:
                        key = str(name)
                        tool_counts[key] = tool_counts.get(key, 0) + 1

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
            trajectory_duration_seconds=(
                max(timestamps) - min(timestamps) if len(timestamps) >= 2 else None
            ),
            total_tool_calls=sum(tool_counts.values()),
            tool_counts=tool_counts,
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


def extract_rewards_map(trial_result: Any) -> dict[str, float] | None:
    """Every named reward from Harbor's verifier result, or None.

    Harbor surfaces the verifier's ``reward.json`` as a dict of named scores;
    RewardKit tasks report each dimension plus their ``reward.toml`` aggregates
    there. ``_extract_reward`` still owns the headline scalar this map is
    collapsed to; the full map is kept (``trials.result["_rewards"]``) so
    multi-dimensional scoring stays visible. A map holding only the headline
    ``reward`` key adds nothing over the scalar and is dropped.
    """
    verifier_result = getattr(trial_result, "verifier_result", None)
    rewards = getattr(verifier_result, "rewards", None)
    if not isinstance(rewards, dict) or set(rewards) <= {"reward"}:
        return None
    sanitized: dict[str, float] = {}
    for key, value in rewards.items():
        if len(sanitized) >= REWARDS_MAP_MAX_KEYS:
            break
        number = _finite_rounded(value)
        if number is None:
            continue
        sanitized[str(key)[:64]] = number
    if not sanitized or set(sanitized) <= {"reward"}:
        return None
    return sanitized


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
        rewards=extract_rewards_map(trial_result),
    )
