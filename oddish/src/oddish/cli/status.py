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
    is_local_api_url,
    require_api_key,
)
from oddish.cli.infra import check_api_health
from oddish.infra import (
    docker_available,
    postgres_container_exists,
    postgres_container_running,
)

console = Console()


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
    is_local_api = is_local_api_url(api_url)

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

        # Health checks
        console.print("[bold cyan]Infrastructure:[/bold cyan]")
        issues = 0

        if is_local_api:
            from oddish.cli.infra import get_db_url_from_env

            db_url = get_db_url_from_env()
            if db_url:
                console.print("  [green]✓[/green] DATABASE_URL configured")
            else:
                if docker_available():
                    console.print("  [green]✓[/green] Docker available")
                else:
                    console.print("  [red]✗[/red] Docker not available")
                    issues += 1

                if postgres_container_exists():
                    if postgres_container_running():
                        console.print("  [green]✓[/green] Postgres running")
                    else:
                        console.print("  [yellow]⚠[/yellow] Postgres stopped")
                        issues += 1
                else:
                    console.print("  [yellow]⚠[/yellow] Postgres not found")
                    issues += 1
        else:
            console.print("  [green]✓[/green] Hosted API configured")

        if check_api_health(api_url):
            console.print("  [green]✓[/green] API healthy")
        else:
            console.print("  [yellow]⚠[/yellow] API not responding")
            issues += 1

        console.print()

        # Recent experiments
        console.print("[bold cyan]Recent Experiments:[/bold cyan]")
        try:
            with httpx.Client(timeout=5.0, headers=get_auth_headers()) as client:
                response = client.get(
                    f"{api_url}/tasks", params={"limit": 200, "offset": 0}
                )

            if response.status_code == 200:
                tasks = response.json()
                if not tasks:
                    console.print("  [dim]No experiments yet[/dim]")
                else:
                    experiments: dict[str, dict] = {}
                    for task in tasks:
                        experiment_id = task.get("experiment_id") or "-"
                        entry = experiments.setdefault(
                            experiment_id,
                            {
                                "experiment_name": task.get("experiment_name") or "-",
                                "tasks": [],
                                "latest_created_at": task.get("created_at") or "",
                            },
                        )
                        entry["tasks"].append(task)
                        created_at = task.get("created_at") or ""
                        if created_at > entry["latest_created_at"]:
                            entry["latest_created_at"] = created_at

                    sorted_experiments = sorted(
                        experiments.items(),
                        key=lambda item: item[1]["latest_created_at"],
                        reverse=True,
                    )

                    table = Table(show_header=True, box=None, padding=(0, 2))
                    table.add_column("Experiment", style="cyan")
                    table.add_column("Name")
                    table.add_column("Tasks", justify="right")
                    table.add_column("Running", justify="right", style="blue")
                    table.add_column("Done", justify="right", style="green")
                    table.add_column("Trials", justify="right")
                    table.add_column("Rewards", justify="right")

                    for experiment_id, entry in sorted_experiments[:8]:
                        exp_tasks = entry["tasks"]
                        total_tasks = len(exp_tasks)
                        running_tasks = sum(
                            1 for t in exp_tasks if t.get("status") == "running"
                        )
                        done_tasks = sum(
                            1
                            for t in exp_tasks
                            if t.get("status") in ("completed", "failed")
                        )
                        total_trials = sum(t.get("total", 0) or 0 for t in exp_tasks)
                        completed_trials = sum(
                            t.get("completed", 0) or 0 for t in exp_tasks
                        )
                        reward_success = sum(
                            t.get("reward_success", 0) or 0 for t in exp_tasks
                        )
                        reward_total = sum(
                            t.get("reward_total", 0) or 0 for t in exp_tasks
                        )

                        trials_display = (
                            f"{completed_trials}/{total_trials}"
                            if total_trials
                            else "-"
                        )
                        rewards_display = (
                            f"{reward_success}/{reward_total}" if reward_total else "-"
                        )

                        table.add_row(
                            experiment_id,
                            entry["experiment_name"],
                            str(total_tasks),
                            str(running_tasks) if running_tasks else "-",
                            str(done_tasks) if done_tasks else "-",
                            trials_display,
                            rewards_display,
                        )

                    console.print(table)
                    console.print(
                        "[dim]Tip: oddish status --experiment <id> --watch[/dim]"
                    )
            else:
                console.print("  [red]Failed to fetch recent experiments[/red]")
        except Exception:
            console.print("  [red]Failed to connect to API[/red]")
            issues += 1

        if verbose:
            console.print()
            console.print(
                "[dim]Pipeline statistics are not available via the CLI anymore.[/dim]"
            )

        console.print()
        if issues > 0:
            console.print(f"[yellow]{issues} issue(s) detected[/yellow]")
            console.print(
                "[dim]Run: oddish run <task_dir> to auto-start infrastructure[/dim]"
            )
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
            reward_pass = sum(1 for t in trials if t.get("reward") == 1)
            reward_fail = sum(1 for t in trials if t.get("reward") == 0)
            if reward_pass > 0 or reward_fail > 0:
                console.print(
                    f"[bold]Rewards:[/bold] [green]{reward_pass} passed[/green], "
                    f"[red]{reward_fail} failed[/red]"
                )

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
                if reward == 1:
                    reward_str = "[green]✓[/green]"
                elif reward == 0:
                    reward_str = "[red]✗[/red]"
                else:
                    reward_str = "-"

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
