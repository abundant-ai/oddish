"""
Format PR comments for Oddish validation results.

Generates markdown comments showing a trajectory analyses matrix
with real-time trial/analysis/verdict progress (sauron-style).
"""

from __future__ import annotations

from oddish.verdict import verdict_label

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TrialSummary:
    """Summary of a single trial for display."""

    index: int
    trial_id: str
    agent: str
    model: str | None
    status: str  # queued, running, paused, success, failed
    reward: float | None
    duration_seconds: float | None
    analysis_status: str | None  # queued, running, success, failed, None
    classification: (
        str | None
    )  # GOOD_SUCCESS, GOOD_FAILURE, BAD_SUCCESS, BAD_FAILURE, HARNESS_ERROR
    subtype: str | None = None
    task_name: str | None = None


@dataclass
class TaskSummary:
    """Summary of a task for display."""

    task_id: str
    task_name: str
    task_url: str
    trials: list[TrialSummary]
    verdict_status: str | None  # pending, running, success, failed, None
    verdict: dict | None
    review_version_matches: bool | None = None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _format_reward(reward: float | None) -> str:
    if reward is None:
        return "-"
    if reward == 1.0:
        return "\u2713"
    if reward == 0.0:
        return "\u2717"
    return f"~ {reward:.2f}"


_CLASSIFICATION_BADGES: dict[str, str] = {
    "GOOD_SUCCESS": "\U0001f7e2 GOOD_SUCCESS",
    "GOOD_FAILURE": "\U0001f7e2 GOOD_FAILURE",
    "BAD_SUCCESS": "\U0001f7e0 BAD_SUCCESS",
    "BAD_FAILURE": "\U0001f7e0 BAD_FAILURE",
    "HARNESS_ERROR": "\u26aa HARNESS_ERROR",
}


def _classification_label(classification: str | None, subtype: str | None) -> str:
    if not classification:
        return "-"
    badge = _CLASSIFICATION_BADGES.get(classification.upper(), classification)
    if subtype:
        return f"{badge} - {subtype}"
    return badge


def _trial_status_cell(trial: TrialSummary) -> str:
    if trial.status in ("queued", "pending"):
        return "\u23f3 Queued"
    if trial.status == "running":
        return "\U0001f504 Running"
    if trial.status == "paused":
        return "\u23f8 Paused"
    if trial.status == "skipped":
        return "\u2298 Skipped"
    if trial.status == "failed":
        return f"\u274c Failed ({_format_duration(trial.duration_seconds)})"
    return f"\u2705 Done ({_format_duration(trial.duration_seconds)})"


def _analysis_cell(
    trial: TrialSummary, dashboard_url: str, experiment_url: str | None = None
) -> str:
    if trial.analysis_status == "success" and trial.classification:
        label = _classification_label(trial.classification, trial.subtype)
        view_url = _trial_view_url(trial, dashboard_url, experiment_url)
        return f"{label} ([View]({view_url}))"
    if trial.analysis_status == "running":
        return "\U0001f504 Analyzing..."
    if trial.analysis_status in ("queued", "pending"):
        return "\u23f3 Pending"
    if trial.analysis_status == "failed":
        return "\u274c Analysis failed"
    if trial.status in ("success", "failed"):
        return "\u23f3 Pending"
    return "-"


def _trial_view_url(
    trial: TrialSummary, dashboard_url: str, experiment_url: str | None = None
) -> str:
    if experiment_url:
        return experiment_url
    return dashboard_url


def _progress_bar(completed: int, total: int) -> str:
    if total == 0:
        return ""
    pct = completed * 100 // total
    filled = completed * 10 // total
    bar = "\u2588" * filled + "\u2591" * (10 - filled)
    return f"`{bar}` {pct}%"


# ---------------------------------------------------------------------------
# Single-task comment
# ---------------------------------------------------------------------------


def format_task_comment(
    task: TaskSummary,
    experiment_name: str,
    experiment_url: str,
    dashboard_url: str = "https://www.oddish.app",
) -> str:
    """Format a complete PR comment for a single task's validation status."""
    lines = [
        "<!-- oddish-validation-results -->",
        "## \U0001f52c Oddish Validation",
        "",
        f"**Task:** [{task.task_name}]({task.task_url})",
        f"**Experiment:** [{experiment_name}]({experiment_url})",
        "",
    ]

    # Skipped counts as "done" (terminal) but isn't analyzable (never ran), so
    # progress and analysis use different denominators. See format_experiment_comment.
    total = len(task.trials)
    skipped = sum(1 for t in task.trials if t.status == "skipped")
    completed = sum(
        1 for t in task.trials if t.status in ("success", "failed", "skipped")
    )
    analyzable = total - skipped
    analyzed = sum(
        1 for t in task.trials if t.analysis_status == "success" and t.classification
    )

    label = verdict_label(
        task.verdict_status, task.verdict, version_matches=task.review_version_matches
    )
    lines.append(f"### Verdict: **{label}**")
    if (
        label in {"Accepted", "Rejected"}
        and task.verdict
        and task.verdict.get("primary_issue")
    ):
        lines.append(f"> {task.verdict['primary_issue']}")
    if analyzable == 0 and completed == total and total > 0:
        # Every trial terminal but nothing analyzable (all gate-skipped) \u2014 show
        # a terminal state, not a stuck "Analyzing Results... (0/0 classified)".
        lines.append(f"### \u2298 All {total} trials skipped (baseline gate)")
    elif analyzable > 0 and analyzed == analyzable:
        lines.append(
            f"### \u23f3 Run analysis complete ({analyzed}/{analyzable} analyses done)"
        )
    elif completed == total and total > 0:
        lines.append(
            f"### \u23f3 Analyzing Results... ({analyzed}/{analyzable} classified)"
        )
    elif completed > 0:
        lines.append(
            f"### \U0001f504 Running \u2014 {completed}/{total} trials complete "
            f"{_progress_bar(completed, total)}"
        )
    else:
        lines.append(f"### \u23f3 Queued ({total} trials)")

    lines.append("")

    lines.append("#### Trajectory Analyses")
    lines.append("")
    lines.append("| # | Agent | Model | Status | Reward | Classification | Analysis |")
    lines.append("|---|-------|-------|--------|--------|----------------|----------|")

    for trial in task.trials:
        status_str = _trial_status_cell(trial)
        reward_str = _format_reward(trial.reward)
        classification_str = _classification_label(trial.classification, trial.subtype)
        analysis_str = _analysis_cell(trial, dashboard_url, experiment_url)
        model_str = trial.model or "-"
        trial_link = f"[{trial.index + 1}]({_trial_view_url(trial, dashboard_url, experiment_url)})"

        lines.append(
            f"| {trial_link} | {trial.agent} | {model_str} | "
            f"{status_str} | {reward_str} | {classification_str} | {analysis_str} |"
        )

    lines.append("")

    if task.verdict and label in {"Accepted", "Rejected"}:
        lines.append("<details>")
        lines.append("<summary>Verdict Details</summary>")
        lines.append("")

        if task.verdict.get("recommendations"):
            lines.append("**Recommendations:**")
            for rec in task.verdict["recommendations"]:
                lines.append(f"- {rec}")
            lines.append("")

        counts = []
        if task.verdict.get("success_count"):
            counts.append(f"\u2705 {task.verdict['success_count']} success")
        if task.verdict.get("task_problem_count"):
            counts.append(
                f"\U0001f534 {task.verdict['task_problem_count']} task issues"
            )
        if task.verdict.get("agent_problem_count"):
            counts.append(
                f"\U0001f7e0 {task.verdict['agent_problem_count']} agent issues"
            )
        if task.verdict.get("harness_error_count"):
            counts.append(
                f"\u26aa {task.verdict['harness_error_count']} harness errors"
            )

        if counts:
            lines.append(f"**Summary:** {' | '.join(counts)}")
            lines.append("")

        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append(
        f"<sub>Powered by [Oddish]({dashboard_url}) \u2022 "
        f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</sub>"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-task / experiment comment
# ---------------------------------------------------------------------------


def format_experiment_comment(
    tasks: list[TaskSummary],
    experiment_name: str,
    experiment_url: str,
    dashboard_url: str = "https://www.oddish.app",
) -> str:
    """Format a PR comment for multiple tasks with a flat trajectory analyses matrix."""
    lines = [
        "<!-- oddish-validation-results -->",
        "## \U0001f52c Oddish Validation",
        "",
        f"**Experiment:** [{experiment_name}]({experiment_url})",
        "",
    ]

    # Skipped counts as "done" (terminal) but isn't analyzable (never ran):
    # progress counts it, analysis stages use analyzable_trials (skipped-excluding).
    total_trials = sum(1 for t in tasks for trial in t.trials)
    skipped_trials = sum(
        1 for t in tasks for trial in t.trials if trial.status == "skipped"
    )
    completed_trials = sum(
        1
        for t in tasks
        for trial in t.trials
        if trial.status in ("success", "failed", "skipped")
    )
    analyzable_trials = total_trials - skipped_trials
    analyzed_trials = sum(
        1
        for t in tasks
        for trial in t.trials
        if trial.analysis_status == "success" and trial.classification
    )
    total_tasks = len(tasks)

    labels = [
        verdict_label(
            t.verdict_status, t.verdict, version_matches=t.review_version_matches
        )
        for t in tasks
    ]
    tasks_with_verdict = [
        t for t, label in zip(tasks, labels) if label in {"Accepted", "Rejected"}
    ]
    good_tasks = labels.count("Accepted")

    if len(tasks_with_verdict) == total_tasks and total_tasks > 0:
        if good_tasks == total_tasks:
            lines.append(f"### \u2705 {total_tasks} Accepted")
        else:
            lines.append(
                f"### \u26a0\ufe0f {good_tasks} Accepted · {total_tasks - good_tasks} Rejected"
            )
    elif (
        analyzable_trials == 0 and completed_trials == total_trials and total_trials > 0
    ):
        # Every trial is terminal but none are analyzable (all gate-skipped),
        # so there's nothing to analyze or verdict \u2014 show a terminal state
        # instead of a stuck "Analyzing results... (0/0 classified)".
        lines.append(f"### \u2298 All {total_trials} trials skipped (baseline gate)")
    elif analyzable_trials > 0 and analyzed_trials == analyzable_trials:
        lines.append(
            f"### \u23f3 Run analysis complete ({analyzed_trials}/{analyzable_trials} analyses done)"
        )
    elif completed_trials == total_trials and total_trials > 0:
        lines.append(
            f"### \u23f3 Analyzing results... ({analyzed_trials}/{analyzable_trials} classified)"
        )
    elif completed_trials > 0:
        lines.append(
            f"### \U0001f504 Progress: {completed_trials}/{total_trials} trials complete "
            f"{_progress_bar(completed_trials, total_trials)}"
        )
    else:
        lines.append(
            f"### \u23f3 Queued ({total_trials} trials across {total_tasks} tasks)"
        )

    lines.append("")

    if tasks:
        lines.append("#### Verdict")
        lines.append("")
        lines.append("| Task | Trials | Verdict |")
        lines.append("|------|--------|---------|")

        for task in tasks:
            task_total = len(task.trials)
            # Skipped is terminal (counts as done), matching the header progress
            # line — otherwise this table reads as still-running for a done,
            # gate-skipped task.
            task_done = sum(
                1 for t in task.trials if t.status in ("success", "failed", "skipped")
            )

            verdict_str = verdict_label(
                task.verdict_status,
                task.verdict,
                version_matches=task.review_version_matches,
            )
            if (
                verdict_str == "Rejected"
                and task.verdict
                and task.verdict.get("primary_issue")
            ):
                verdict_str += f" — {task.verdict['primary_issue']}"

            lines.append(
                f"| [{task.task_name}]({task.task_url}) | {task_done}/{task_total} | {verdict_str} |"
            )

        lines.append("")

    lines.append("#### Trajectory Analyses")
    lines.append("")
    lines.append(
        "Analysis of agent trajectories including baseline validation and outcome classification."
    )
    lines.append("")
    lines.append("| Task | Agent | Model | Attempt | Classification | Analysis |")
    lines.append("|------|-------|-------|---------|----------------|----------|")

    for task in tasks:
        for trial in task.trials:
            model_str = trial.model or "-"
            classification_str = _classification_label(
                trial.classification, trial.subtype
            )
            analysis_str = _analysis_cell(trial, dashboard_url, experiment_url)
            trial_link = f"[{trial.index + 1}]({_trial_view_url(trial, dashboard_url, experiment_url)})"

            lines.append(
                f"| {task.task_name} | {trial.agent} | {model_str} | "
                f"{trial_link} | {classification_str} | {analysis_str} |"
            )

    lines.append("")

    lines.append("---")
    lines.append(
        f"<sub>Powered by [Oddish]({dashboard_url}) \u2022 "
        f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</sub>"
    )

    return "\n".join(lines)
