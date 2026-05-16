from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.api import (
    format_task_status,
    format_trial_status,
    print_experiment_status,
    watch_experiment,
    watch_task,
)
from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    require_api_key,
)

console = Console()

SYSTEM_STATUS_TIMEOUT_SECONDS = 30.0
RECENT_EXPERIMENTS_LIMIT = 8


def _format_reward_display(reward: float | None) -> str:
    if reward is None:
        return "-"
    if reward == 1:
        return "[green]✓[/green]"
    if reward == 0:
        return "[red]✗[/red]"
    return f"[yellow]{reward:.2f}[/yellow]"


def _fetch_dashboard_experiments(
    client: httpx.Client,
    api_url: str,
    *,
    experiments_status: str,
) -> httpx.Response:
    return client.get(
        f"{api_url}/dashboard",
        params={
            "include_tasks": "false",
            "include_usage": "false",
            "include_experiments": "true",
            "experiments_limit": RECENT_EXPERIMENTS_LIMIT,
            "experiments_offset": 0,
            "experiments_status": experiments_status,
        },
    )


def status(
    task_id: Annotated[
        Optional[str],
        typer.Argument(
            help="Task ID to check (omit to see system status or use --experiment)"
        ),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment ID to monitor (cannot be used with task_id)",
        ),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            "-w",
            help="Watch progress until completion (task or experiment)",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed pipeline statistics (system status only)",
        ),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
):
    """Check system, task, or experiment status.

    Without arguments: Shows system health and queue statistics.
    With task_id: Shows specific task progress including pipeline stage.
    With --experiment: Shows all tasks within an experiment.

    Examples:
        oddish status                   # System overview
        oddish status -v                # System overview with pipeline stats
        oddish status <task_id>         # Task details
        oddish status <task_id> --watch # Live task monitoring
        oddish status --experiment <experiment_id>
        oddish status --experiment <experiment_id> --watch
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if task_id and experiment_id:
        console.print("[red]Provide either a task_id or --experiment, not both.[/red]")
        raise typer.Exit(1)

    if experiment_id:
        if watch:
            watch_experiment(api_url, experiment_id)
        else:
            print_experiment_status(api_url, experiment_id)
        return

    # No task_id: show system status (health + queues)
    if task_id is None:
        console.print("[bold]Oddish System Status[/bold]\n")

        console.print("[bold cyan]Infrastructure:[/bold cyan]")
        issues = 0

        console.print()

        # Active experiments first; recent history is only a fallback.
        console.print("[bold cyan]Active Experiments:[/bold cyan]")
        try:
            with httpx.Client(
                timeout=SYSTEM_STATUS_TIMEOUT_SECONDS,
                headers=get_auth_headers(api_url),
            ) as client:
                response = _fetch_dashboard_experiments(
                    client,
                    api_url,
                    experiments_status="active",
                )
                using_recent_fallback = False
                if response.status_code == 200 and not (
                    response.json().get("experiments") or []
                ):
                    using_recent_fallback = True
                    response = _fetch_dashboard_experiments(
                        client,
                        api_url,
                        experiments_status="all",
                    )

            if response.status_code == 200:
                experiments = response.json().get("experiments") or []
                if not experiments:
                    console.print("  [dim]No active or recent experiments[/dim]")
                else:
                    if using_recent_fallback:
                        console.print("  [dim]No active experiments; showing recent.[/dim]")
                    table = Table(show_header=True, box=None, padding=(0, 2))
                    table.add_column("Experiment", style="cyan")
                    table.add_column("Name")
                    table.add_column("Tasks", justify="right")
                    table.add_column("Active", justify="right", style="blue")
                    table.add_column("Trials", justify="right")
                    table.add_column("Rewards", justify="right")

                    for experiment in experiments:
                        total_tasks = experiment.get("task_count", 0) or 0
                        active_trials = experiment.get("active_trials", 0) or 0
                        total_trials = experiment.get("total_trials", 0) or 0
                        completed_trials = experiment.get("completed_trials", 0) or 0
                        reward_success = experiment.get("reward_success", 0) or 0
                        reward_total = experiment.get("reward_total", 0) or 0

                        trials_display = (
                            f"{completed_trials}/{total_trials}"
                            if total_trials
                            else "-"
                        )
                        rewards_display = (
                            f"{reward_success}/{reward_total}" if reward_total else "-"
                        )

                        table.add_row(
                            experiment.get("id") or "-",
                            experiment.get("name") or "-",
                            str(total_tasks),
                            str(active_trials) if active_trials else "-",
                            trials_display,
                            rewards_display,
                        )

                    console.print(table)
                    console.print(
                        "[dim]Tip: oddish status --experiment <id> --watch[/dim]"
                    )
            else:
                console.print(
                    "  [red]Failed to fetch experiment status:[/red] "
                    f"HTTP {response.status_code}: {response.text}"
                )
                issues += 1
        except httpx.TimeoutException as exc:
            console.print(f"  [red]Timed out fetching experiment status:[/red] {exc}")
            issues += 1
        except httpx.HTTPError as exc:
            console.print(f"  [red]Failed to fetch experiment status:[/red] {exc}")
            issues += 1
        except Exception as exc:
            console.print(f"  [red]Failed to fetch experiment status:[/red] {exc}")
            issues += 1

        if verbose:
            console.print()
            console.print(
                "[dim]Pipeline statistics are not available via the CLI anymore.[/dim]"
            )

        console.print()
        if issues > 0:
            console.print(f"[yellow]{issues} issue(s) detected[/yellow]")
        else:
            console.print("[green]All systems operational ✓[/green]")

        return

    # Task_id provided: show task status (or experiment fallback)
    if watch:
        with httpx.Client(headers=get_auth_headers()) as client:
            response = client.get(f"{api_url}/tasks/{task_id}")

        if response.status_code == 404:
            watch_experiment(api_url, task_id)
            return
        if response.status_code != 200:
            console.print(f"[red]Failed to get status:[/red] {response.text}")
            return

        try:
            watch_task(api_url, task_id)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped watching[/dim]")
        return

    with httpx.Client(headers=get_auth_headers()) as client:
        response = client.get(f"{api_url}/tasks/{task_id}")

    if response.status_code == 200:
        result = response.json()

        # Task header
        task_status = result.get("status", "unknown")
        status_display = format_task_status(task_status)

        console.print(f"[bold]Task:[/bold] {result['id']}")
        console.print(f"[bold]Experiment:[/bold] {result.get('experiment_name', '-')}")
        console.print(f"[bold]Status:[/bold] {status_display}")
        console.print(f"[bold]Progress:[/bold] {result['progress']}")

        # Show reward summary
        trials = result.get("trials", [])
        if trials:
            rewards = [
                float(t["reward"]) for t in trials if t.get("reward") is not None
            ]
            reward_pass = sum(1 for reward in rewards if reward == 1)
            reward_fail = sum(1 for reward in rewards if reward == 0)
            partial_scores = sum(1 for reward in rewards if 0 < reward < 1)
            if rewards:
                summary = [f"avg [cyan]{sum(rewards) / len(rewards):.2f}[/cyan]"]
                if reward_pass > 0:
                    summary.append(f"[green]{reward_pass} perfect[/green]")
                if partial_scores > 0:
                    summary.append(f"[yellow]{partial_scores} partial[/yellow]")
                if reward_fail > 0:
                    summary.append(f"[red]{reward_fail} zero[/red]")
                console.print("[bold]Rewards:[/bold] " + ", ".join(summary))

        # Show verdict if available
        verdict_status = result.get("verdict_status")
        if verdict_status:
            verdict_display = {
                "pending": "[dim]pending[/dim]",
                "queued": "[yellow]queued[/yellow]",
                "running": "[blue]running[/blue]",
                "success": "[green]done[/green]",
                "failed": "[red]failed[/red]",
            }.get(verdict_status.lower(), verdict_status)
            console.print(f"[bold]Verdict:[/bold] {verdict_display}")

            # Show verdict summary if completed
            verdict = result.get("verdict")
            if verdict and isinstance(verdict, dict):
                summary = verdict.get("summary") or verdict.get("recommendation")
                if summary:
                    console.print(
                        f"  [dim]{summary[:100]}...[/dim]"
                        if len(str(summary)) > 100
                        else f"  [dim]{summary}[/dim]"
                    )

        console.print()

        if trials:
            table = Table(title="Trials")
            table.add_column("#", style="cyan", justify="right")
            table.add_column("Agent")
            table.add_column("Model")
            table.add_column("Status")
            table.add_column("Stage", style="dim")
            table.add_column("Reward", justify="center")
            table.add_column("Attempts", justify="center")

            for trial in trials:
                trial_idx = trial["id"].split("-")[-1]
                trial_status = trial["status"]
                harbor_stage = trial.get("harbor_stage") or "-"
                trial_status_display = format_trial_status(trial_status)

                reward = trial.get("reward")
                reward_str = _format_reward_display(
                    float(reward) if reward is not None else None
                )

                attempts = trial.get("attempts", 0)
                max_attempts = trial.get("max_attempts", 6)

                # Show analysis status if available
                analysis_status = trial.get("analysis_status")
                if analysis_status and analysis_status not in ("pending", None):
                    analysis_display = {
                        "queued": "[yellow]A:q[/yellow]",
                        "running": "[blue]A:run[/blue]",
                        "success": "[green]A:✓[/green]",
                        "failed": "[red]A:✗[/red]",
                    }.get(analysis_status.lower(), "")
                    if analysis_display:
                        trial_status_display = (
                            f"{trial_status_display} {analysis_display}"
                        )

                table.add_row(
                    trial_idx,
                    trial["agent"],
                    trial.get("model") or "-",
                    trial_status_display,
                    harbor_stage if trial_status == "running" else "-",
                    reward_str,
                    f"{attempts}/{max_attempts}",
                )
            console.print(table)
    elif response.status_code == 404:
        if print_experiment_status(api_url, task_id):
            return
        console.print(f"[red]Failed to get status:[/red] {response.text}")
    else:
        console.print(f"[red]Failed to get status:[/red] {response.text}")
