"""
Format PR comments for Oddish validation results.

Generates markdown comments showing a trajectory analyses matrix
with real-time trial/analysis/verdict progress (sauron-style).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TrialSummary:
    """Summary of a single trial for display."""

    index: int
    trial_id: str
    agent: str
    model: str | None
    status: str  # queued, running, success, failed
    reward: float | None
    duration_seconds: float | None
    analysis_status: str | None  # queued, running, success, failed, None
    classification: (
        str | None
    )  # GOOD_SUCCESS, GOOD_FAILURE, BAD_SUCCESS, BAD_FAILURE, HARNESS_ERROR
    subtype: str | None = None  # e.g. "Premature Stop", "Underspecified Instruction"
    task_name: str | None = None  # Populated for flat experiment-level views


@dataclass
class TaskSummary:
    """Summary of a task for display."""

    task_id: str
    task_name: str
    task_url: str
    trials: list[TrialSummary]
    verdict_status: str | None  # pending, running, success, failed, None
    verdict: dict | None  # The verdict result if available


def _status_emoji(status: str | None) -> str:
    """Map status to emoji."""
    return {
        "pending": "⏳",
        "queued": "⏳",
        "running": "🔄",
        "success": "✅",
        "failed": "❌",
        "retrying": "🔁",
    }.get(status or "", "❓")


def _format_duration(seconds: float | None) -> str:
    """Format duration in human-readable form."""
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
    """Format reward value."""
    if reward is None:
        return "-"
    return "✓" if reward >= 0.5 else "✗"


_CLASSIFICATION_BADGES: dict[str, str] = {
    "GOOD_SUCCESS": "🟢 GOOD_SUCCESS",
    "GOOD_FAILURE": "🟢 GOOD_FAILURE",
    "BAD_SUCCESS": "🟠 BAD_SUCCESS",
    "BAD_FAILURE": "🟠 BAD_FAILURE",
    "HARNESS_ERROR": "⚪ HARNESS_ERROR",
}


def _classification_label(classification: str | None, subtype: str | None) -> str:
    """Format classification + subtype as a label (e.g. 'GOOD_FAILURE - Premature Stop')."""
    if not classification:
        return "-"
    badge = _CLASSIFICATION_BADGES.get(classification.upper(), classification)
    if subtype:
        return f"{badge} - {subtype}"
    return badge


def _trial_status_cell(trial: TrialSummary) -> str:
    """Render a trial's combined status (run + analysis) as a compact cell."""
    if trial.status in ("queued", "pending"):
        return "⏳ Queued"
    if trial.status == "running":
        return "🔄 Running"
    if trial.status == "failed":
        return f"❌ Failed ({_format_duration(trial.duration_seconds)})"
    # status == "success"
    return f"✅ Done ({_format_duration(trial.duration_seconds)})"


def _analysis_cell(
    trial: TrialSummary, dashboard_url: str, experiment_url: str | None = None
) -> str:
    """Render the analysis/classification cell with an optional View link."""
    if trial.analysis_status == "success" and trial.classification:
        label = _classification_label(trial.classification, trial.subtype)
        view_url = _trial_view_url(trial, dashboard_url, experiment_url)
        return f"{label} ([View]({view_url}))"
    if trial.analysis_status == "running":
        return "🔄 Analyzing..."
    if trial.analysis_status in ("queued", "pending"):
        return "⏳ Pending"
    if trial.analysis_status == "failed":
        return "❌ Analysis failed"
    if trial.status in ("success", "failed"):
        return "⏳ Pending"
    return "-"


def _trial_view_url(
    trial: TrialSummary, dashboard_url: str, experiment_url: str | None = None
) -> str:
    """Build a dashboard URL pointing to a specific trial's experiment."""
    if experiment_url:
        return experiment_url
    return dashboard_url


def _progress_bar(completed: int, total: int) -> str:
    """Render a text-based progress indicator."""
    if total == 0:
        return ""
    pct = completed * 100 // total
    filled = completed * 10 // total
    bar = "█" * filled + "░" * (10 - filled)
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
        "## 🔬 Oddish Validation",
        "",
        f"**Task:** [{task.task_name}]({task.task_url})",
        f"**Experiment:** [{experiment_name}]({experiment_url})",
        "",
    ]

    total = len(task.trials)
    completed = sum(1 for t in task.trials if t.status in ("success", "failed"))
    analyzed = sum(
        1 for t in task.trials if t.analysis_status == "success" and t.classification
    )

    # Verdict banner
    if task.verdict_status == "success" and task.verdict:
        verdict_emoji = "✅" if task.verdict.get("is_good") else "⚠️"
        verdict_text = "GOOD" if task.verdict.get("is_good") else "NEEDS REVIEW"
        lines.append(f"### {verdict_emoji} Verdict: **{verdict_text}**")
        if task.verdict.get("primary_issue"):
            lines.append(f"> {task.verdict['primary_issue']}")
    elif task.verdict_status == "running":
        lines.append("### 🔄 Computing Verdict...")
    elif analyzed == total and total > 0:
        lines.append(f"### ⏳ Computing Verdict... ({analyzed}/{total} analyses done)")
    elif completed == total and total > 0:
        lines.append(f"### ⏳ Analyzing Results... ({analyzed}/{total} classified)")
    elif completed > 0:
        lines.append(
            f"### 🔄 Running — {completed}/{total} trials complete "
            f"{_progress_bar(completed, total)}"
        )
    else:
        lines.append(f"### ⏳ Queued ({total} trials)")

    lines.append("")

    # Trajectory analyses matrix
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

    # Verdict details
    if task.verdict and task.verdict_status == "success":
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
            counts.append(f"✅ {task.verdict['success_count']} success")
        if task.verdict.get("task_problem_count"):
            counts.append(f"🔴 {task.verdict['task_problem_count']} task issues")
        if task.verdict.get("agent_problem_count"):
            counts.append(f"🟠 {task.verdict['agent_problem_count']} agent issues")
        if task.verdict.get("harness_error_count"):
            counts.append(f"⚪ {task.verdict['harness_error_count']} harness errors")

        if counts:
            lines.append(f"**Summary:** {' | '.join(counts)}")
            lines.append("")

        lines.append("</details>")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(
        f"<sub>Powered by [Oddish]({dashboard_url}) • "
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
        "## 🔬 Oddish Validation",
        "",
        f"**Experiment:** [{experiment_name}]({experiment_url})",
        "",
    ]

    total_trials = sum(len(t.trials) for t in tasks)
    completed_trials = sum(
        1 for t in tasks for trial in t.trials if trial.status in ("success", "failed")
    )
    analyzed_trials = sum(
        1
        for t in tasks
        for trial in t.trials
        if trial.analysis_status == "success" and trial.classification
    )
    total_tasks = len(tasks)

    tasks_with_verdict = [
        t for t in tasks if t.verdict_status == "success" and t.verdict
    ]
    good_tasks = sum(1 for t in tasks_with_verdict if t.verdict.get("is_good"))

    # Overall status
    if len(tasks_with_verdict) == total_tasks and total_tasks > 0:
        if good_tasks == total_tasks:
            lines.append(f"### ✅ All {total_tasks} tasks passed validation")
        else:
            lines.append(f"### ⚠️ {good_tasks}/{total_tasks} tasks passed validation")
    elif analyzed_trials == total_trials and total_trials > 0:
        lines.append(
            f"### ⏳ Computing verdicts... ({analyzed_trials}/{total_trials} analyses done)"
        )
    elif completed_trials == total_trials and total_trials > 0:
        lines.append(
            f"### ⏳ Analyzing results... ({analyzed_trials}/{total_trials} classified)"
        )
    elif completed_trials > 0:
        lines.append(
            f"### 🔄 Progress: {completed_trials}/{total_trials} trials complete "
            f"{_progress_bar(completed_trials, total_trials)}"
        )
    else:
        lines.append(
            f"### ⏳ Queued ({total_trials} trials across {total_tasks} tasks)"
        )

    lines.append("")

    # Per-task verdict summary (compact)
    if any(t.verdict_status for t in tasks):
        lines.append("#### Task Verdicts")
        lines.append("")
        lines.append("| Task | Trials | Verdict |")
        lines.append("|------|--------|---------|")

        for task in tasks:
            task_total = len(task.trials)
            task_done = sum(1 for t in task.trials if t.status in ("success", "failed"))

            if task.verdict_status == "success" and task.verdict:
                verdict_emoji = "✅" if task.verdict.get("is_good") else "⚠️"
                verdict_str = f"{verdict_emoji} {'Good' if task.verdict.get('is_good') else 'Review'}"
                if task.verdict.get("primary_issue"):
                    verdict_str += f" — {task.verdict['primary_issue']}"
            elif task.verdict_status == "running":
                verdict_str = "🔄 Computing..."
            elif task_done == task_total and task_total > 0:
                verdict_str = "⏳ Pending"
            else:
                verdict_str = f"🔄 {task_done}/{task_total} trials done"

            lines.append(
                f"| [{task.task_name}]({task.task_url}) | {task_done}/{task_total} | {verdict_str} |"
            )

        lines.append("")

    # Flat trajectory analyses matrix across all tasks
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

    # Footer
    lines.append("---")
    lines.append(
        f"<sub>Powered by [Oddish]({dashboard_url}) • "
        f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</sub>"
    )

    return "\n".join(lines)
