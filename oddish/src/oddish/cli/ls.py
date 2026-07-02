from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()

# Rolling "Created" presets, mirroring the frontend (tasks-filters.ts). The
# backend has no ``created_within`` param, so the CLI resolves the token to a
# ``created_after`` bound relative to now — the same way the UI loader does.
_CREATED_WITHIN = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%m-%d %H:%M")


def _format_reward(row: dict[str, Any]) -> str:
    reward_total = int(row.get("reward_total") or 0)
    if reward_total == 0:
        return "-"

    reward_success = int(row.get("reward_success") or 0)
    reward_sum = float(row.get("reward_sum") or 0)
    average = reward_sum / reward_total
    return f"{reward_success}/{reward_total} avg {average:.2f}"


def _format_trials(row: dict[str, Any]) -> str:
    total = int(row.get("total_trials") or 0)
    completed = int(row.get("completed_trials") or 0)
    failed = int(row.get("failed_trials") or 0)
    if total == 0:
        return "-"
    if failed:
        label = "fail" if failed == 1 else "fails"
        return f"{completed}/{total} ({failed} {label})"
    return f"{completed}/{total}"


def _format_experiments(row: dict[str, Any]) -> str:
    experiments = row.get("experiments") or []
    if not experiments:
        return "-"
    names = [
        experiment.get("name") or experiment.get("id") or "-"
        for experiment in experiments
    ]
    return ", ".join(names[:2]) + (" +" if len(names) > 2 else "")


def ls(
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            "-q",
            help="Filter tasks by name",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            max=100,
            help="Maximum number of tasks to show",
        ),
    ] = 25,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of tasks to skip",
        ),
    ] = 0,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the raw JSON response",
        ),
    ] = False,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Require this tag (repeatable; AND semantics).",
        ),
    ] = None,
    tag_any: Annotated[
        list[str] | None,
        typer.Option(
            "--tag-any",
            help="Match any of these tags (repeatable; OR semantics).",
        ),
    ] = None,
    not_tag: Annotated[
        list[str] | None,
        typer.Option(
            "--not-tag",
            help="Exclude tasks that carry any of these tags (repeatable).",
        ),
    ] = None,
    # --- Task-column filters (repeatable → CSV) ---
    status: Annotated[
        list[str] | None,
        typer.Option("--status", help="Task status (repeatable)."),
    ] = None,
    priority: Annotated[
        list[str] | None,
        typer.Option("--priority", help="Task priority (repeatable)."),
    ] = None,
    verdict_status: Annotated[
        list[str] | None,
        typer.Option("--verdict-status", help="Task verdict status (repeatable)."),
    ] = None,
    experiment_id: Annotated[
        list[str] | None,
        typer.Option("--experiment-id", help="Experiment id (repeatable)."),
    ] = None,
    # --- Trial-level filters (repeatable → CSV) ---
    agent: Annotated[
        list[str] | None,
        typer.Option("--agent", help="Trial agent (repeatable)."),
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="Trial model (repeatable)."),
    ] = None,
    agent_model: Annotated[
        list[str] | None,
        typer.Option(
            "--agent-model",
            help="Agent+model pair as 'agent:model' (repeatable).",
        ),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Trial provider (repeatable)."),
    ] = None,
    environment: Annotated[
        list[str] | None,
        typer.Option("--environment", help="Trial environment (repeatable)."),
    ] = None,
    trial_status: Annotated[
        list[str] | None,
        typer.Option("--trial-status", help="Trial status (repeatable)."),
    ] = None,
    origin: Annotated[
        list[str] | None,
        typer.Option("--origin", help="Trial origin (repeatable)."),
    ] = None,
    analysis_classification: Annotated[
        list[str] | None,
        typer.Option(
            "--analysis-classification",
            help="Trial analysis classification (repeatable).",
        ),
    ] = None,
    harbor_sha: Annotated[
        list[str] | None,
        typer.Option("--harbor-sha", help="Harbor SHA (repeatable)."),
    ] = None,
    harbor_stage: Annotated[
        list[str] | None,
        typer.Option("--harbor-stage", help="Harbor stage (repeatable)."),
    ] = None,
    # --- Tri-state booleans (--x / --no-x; unset omits) ---
    has_link: Annotated[
        bool | None,
        typer.Option("--has-link/--no-has-link", help="Task has a source link."),
    ] = None,
    has_error: Annotated[
        bool | None,
        typer.Option("--has-error/--no-has-error", help="Trial has an error."),
    ] = None,
    has_trajectory: Annotated[
        bool | None,
        typer.Option(
            "--has-trajectory/--no-has-trajectory", help="Trial has a trajectory."
        ),
    ] = None,
    trial_is_probe: Annotated[
        bool | None,
        typer.Option(
            "--trial-is-probe/--no-trial-is-probe", help="Match probe trials."
        ),
    ] = None,
    run_analysis: Annotated[
        bool | None,
        typer.Option("--run-analysis/--no-run-analysis", help="Task runs analysis."),
    ] = None,
    run_probe: Annotated[
        bool | None,
        typer.Option("--run-probe/--no-run-probe", help="Task runs probe."),
    ] = None,
    # --- Datetimes ---
    created_after: Annotated[
        datetime | None,
        typer.Option("--created-after", help="Only tasks created after this time."),
    ] = None,
    created_before: Annotated[
        datetime | None,
        typer.Option("--created-before", help="Only tasks created before this time."),
    ] = None,
    created_within: Annotated[
        str | None,
        typer.Option(
            "--created-within",
            help="Rolling window: 24h, 7d, or 30d (resolved to created-after).",
        ),
    ] = None,
    # --- Per-trial numeric ranges ---
    min_attempts: Annotated[
        int | None, typer.Option("--min-attempts", min=1)
    ] = None,
    min_tokens: Annotated[int | None, typer.Option("--min-tokens", min=0)] = None,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens", min=0)] = None,
    min_steps: Annotated[int | None, typer.Option("--min-steps", min=0)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps", min=0)] = None,
    reward_min: Annotated[
        float | None, typer.Option("--reward-min", min=0.0, max=1.0)
    ] = None,
    reward_max: Annotated[
        float | None, typer.Option("--reward-max", min=0.0, max=1.0)
    ] = None,
    # --- Task aggregate ranges (computed on the fly) ---
    avg_score_min: Annotated[
        float | None,
        typer.Option("--avg-score-min", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    avg_score_max: Annotated[
        float | None,
        typer.Option("--avg-score-max", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    total_tokens_min: Annotated[
        int | None, typer.Option("--total-tokens-min", min=0)
    ] = None,
    total_tokens_max: Annotated[
        int | None, typer.Option("--total-tokens-max", min=0)
    ] = None,
    runtime_total_min: Annotated[
        float | None,
        typer.Option("--runtime-total-min", min=0.0, help="Seconds."),
    ] = None,
    runtime_total_max: Annotated[
        float | None,
        typer.Option("--runtime-total-max", min=0.0, help="Seconds."),
    ] = None,
    runtime_avg_min: Annotated[
        float | None,
        typer.Option("--runtime-avg-min", min=0.0, help="Seconds per trial."),
    ] = None,
    runtime_avg_max: Annotated[
        float | None,
        typer.Option("--runtime-avg-max", min=0.0, help="Seconds per trial."),
    ] = None,
    total_trials_min: Annotated[
        int | None, typer.Option("--total-trials-min", min=1)
    ] = None,
    completed_trials_min: Annotated[
        int | None, typer.Option("--completed-trials-min", min=1)
    ] = None,
    failed_trials_min: Annotated[
        int | None, typer.Option("--failed-trials-min", min=1)
    ] = None,
    pass_count_min: Annotated[
        int | None, typer.Option("--pass-count-min", min=1)
    ] = None,
    partial_count_min: Annotated[
        int | None, typer.Option("--partial-count-min", min=1)
    ] = None,
    fail_count_min: Annotated[
        int | None, typer.Option("--fail-count-min", min=1)
    ] = None,
    harness_count_min: Annotated[
        int | None, typer.Option("--harness-count-min", min=1)
    ] = None,
    pass_rate_min: Annotated[
        float | None,
        typer.Option("--pass-rate-min", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    pass_rate_max: Annotated[
        float | None,
        typer.Option("--pass-rate-max", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    # --- Aggregate sort ---
    sort: Annotated[
        str | None,
        typer.Option(
            "--sort",
            help=(
                "avg_score_(asc|desc), total_tokens_(asc|desc), "
                "runtime_total_(asc|desc), runtime_avg_(asc|desc)."
            ),
        ),
    ] = None,
    # --- Agent/model comparison (A beats B on a metric) ---
    compare_by: Annotated[
        str | None,
        typer.Option("--compare-by", help="Compare subject: agent or model."),
    ] = None,
    compare_a: Annotated[
        str | None, typer.Option("--compare-a", help="Subject A name.")
    ] = None,
    compare_b: Annotated[
        str | None, typer.Option("--compare-b", help="Subject B name.")
    ] = None,
    compare_metric: Annotated[
        str | None,
        typer.Option(
            "--compare-metric",
            help="reward | runtime | tokens | steps | pass_rate.",
        ),
    ] = None,
    compare_agg: Annotated[
        str | None,
        typer.Option("--compare-agg", help="best | avg | median."),
    ] = None,
    compare_margin: Annotated[
        float | None, typer.Option("--compare-margin", min=0.0)
    ] = None,
    compare_margin_unit: Annotated[
        str | None,
        typer.Option("--compare-margin-unit", help="pct (default) or abs."),
    ] = None,
    # --- Top performer (best subject per task) ---
    top_by: Annotated[
        str | None,
        typer.Option("--top-by", help="Top performer subject: agent or model."),
    ] = None,
    top_value: Annotated[
        str | None,
        typer.Option("--top-value", help="Subject that must be the top performer."),
    ] = None,
    top_metric: Annotated[
        str | None,
        typer.Option(
            "--top-metric",
            help="reward | runtime | tokens | steps | pass_rate.",
        ),
    ] = None,
    # --- OR-groups ("Match any of…") ---
    or_groups: Annotated[
        str | None,
        typer.Option(
            "--or-groups",
            help="Raw JSON list of condition dicts (same keys as the flat params).",
        ),
    ] = None,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
) -> None:
    """List uploaded tasks."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    params: dict[str, Any] = {"limit": limit, "offset": offset}

    def _csv(name: str, values: list[str] | None) -> None:
        if values:
            params[name] = ",".join(values)

    def _bool(name: str, value: bool | None) -> None:
        if value is not None:
            params[name] = "true" if value else "false"

    def _num(name: str, value: int | float | None) -> None:
        if value is not None:
            params[name] = value

    def _str(name: str, value: str | None) -> None:
        if value:
            params[name] = value

    if query:
        params["query"] = query
    _csv("tags", tag)
    _csv("tags_any", tag_any)
    _csv("tags_none", not_tag)
    _csv("statuses", status)
    _csv("priorities", priority)
    _csv("verdict_statuses", verdict_status)
    _csv("experiment_ids", experiment_id)
    _csv("agents", agent)
    _csv("models", model)
    _csv("agent_models", agent_model)
    _csv("providers", provider)
    _csv("environments", environment)
    _csv("trial_statuses", trial_status)
    _csv("origins", origin)
    _csv("analysis_classifications", analysis_classification)
    _csv("harbor_shas", harbor_sha)
    _csv("harbor_stages", harbor_stage)
    _bool("has_link", has_link)
    _bool("has_error", has_error)
    _bool("has_trajectory", has_trajectory)
    _bool("trial_is_probe", trial_is_probe)
    _bool("run_analysis", run_analysis)
    _bool("run_probe", run_probe)
    if created_before:
        params["created_before"] = created_before.isoformat()
    # A rolling preset owns ``created_after`` (mirrors the UI loader): resolve the
    # window to (now - delta); an explicit --created-after is used otherwise.
    if created_within is not None:
        if created_within not in _CREATED_WITHIN:
            raise typer.BadParameter(
                "must be one of: " + ", ".join(_CREATED_WITHIN),
                param_hint="--created-within",
            )
        resolved = datetime.now(timezone.utc) - _CREATED_WITHIN[created_within]
        params["created_after"] = resolved.isoformat()
    elif created_after:
        params["created_after"] = created_after.isoformat()
    _num("min_attempts", min_attempts)
    _num("min_tokens", min_tokens)
    _num("max_tokens", max_tokens)
    _num("min_steps", min_steps)
    _num("max_steps", max_steps)
    _num("reward_min", reward_min)
    _num("reward_max", reward_max)
    _num("avg_score_min", avg_score_min)
    _num("avg_score_max", avg_score_max)
    _num("total_tokens_min", total_tokens_min)
    _num("total_tokens_max", total_tokens_max)
    _num("runtime_total_min", runtime_total_min)
    _num("runtime_total_max", runtime_total_max)
    _num("runtime_avg_min", runtime_avg_min)
    _num("runtime_avg_max", runtime_avg_max)
    _num("total_trials_min", total_trials_min)
    _num("completed_trials_min", completed_trials_min)
    _num("failed_trials_min", failed_trials_min)
    _num("pass_count_min", pass_count_min)
    _num("partial_count_min", partial_count_min)
    _num("fail_count_min", fail_count_min)
    _num("harness_count_min", harness_count_min)
    _num("pass_rate_min", pass_rate_min)
    _num("pass_rate_max", pass_rate_max)
    _str("sort", sort)
    _str("compare_by", compare_by)
    _str("compare_a", compare_a)
    _str("compare_b", compare_b)
    _str("compare_metric", compare_metric)
    _str("compare_agg", compare_agg)
    _num("compare_margin", compare_margin)
    _str("compare_margin_unit", compare_margin_unit)
    _str("top_by", top_by)
    _str("top_value", top_value)
    _str("top_metric", top_metric)
    _str("or_groups", or_groups)

    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.get(f"{api_url}/tasks/browse", params=params)
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code != 200:
        console.print(f"[red]Failed to list tasks:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()
    if json_output:
        print(json.dumps(result, indent=2))
        return

    tasks = result.get("items") or []
    if not tasks:
        console.print("[dim]No tasks found[/dim]")
        return

    table = Table(title="Tasks", show_header=True)
    table.add_column("Task", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Ver", justify="right", no_wrap=True)
    table.add_column("Trials", justify="right", no_wrap=True)
    table.add_column("Reward", justify="right", no_wrap=True)
    table.add_column("Last", no_wrap=True)
    table.add_column("Exp")
    table.add_column("Tags")

    for task in tasks:
        current_version = task.get("current_version")
        version = f"v{current_version}" if current_version is not None else "-"
        user_tags = task.get("user_tags") or []
        primary = [t["key"] for t in user_tags if t.get("current")]
        older_only = [
            t["key"] for t in user_tags if t.get("older") and not t.get("current")
        ]
        tag_label = ", ".join(primary)
        if older_only:
            older_str = "(" + ", ".join(older_only) + ")"
            tag_label = f"{tag_label} {older_str}" if tag_label else older_str
        table.add_row(
            task.get("id", "-"),
            task.get("name") or "-",
            version,
            _format_trials(task),
            _format_reward(task),
            _format_datetime(task.get("last_run_at")),
            _format_experiments(task),
            tag_label or "-",
        )

    console.print(table)
    if result.get("has_more"):
        next_offset = offset + limit
        console.print(f"[dim]More available: oddish ls --offset {next_offset}[/dim]")
