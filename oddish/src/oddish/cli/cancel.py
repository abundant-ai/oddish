from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
)

console = Console()


def cancel(
    task_id: Annotated[
        Optional[str],
        typer.Argument(
            help="Task ID to cancel (or use --experiment for experiment)"
        ),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment ID to cancel (cancels all tasks in the experiment)",
        ),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-u",
            help="API URL (uses configured URL if not specified)",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip confirmation prompt",
        ),
    ] = False,
):
    """Cancel a running task or experiment.

    Cancels queued and running trials without deleting any data.
    Completed or failed trials are not affected.

    Examples:
        oddish cancel <task_id>            # Cancel a specific task
        oddish cancel --experiment <id>    # Cancel all tasks in an experiment
        oddish cancel <task_id> --yes      # Cancel without confirmation
    """
    if not api_url:
        api_url = get_api_url()

    if task_id and experiment_id:
        console.print("[red]Provide either a task_id or --experiment, not both.[/red]")
        raise typer.Exit(1)

    if not task_id and not experiment_id:
        console.print("[red]Provide a task_id or use --experiment.[/red]")
        raise typer.Exit(1)

    # Confirm cancellation
    if not yes:
        if task_id:
            confirm = typer.confirm(
                f"Cancel task {task_id} and its running trials?", default=False
            )
        else:
            confirm = typer.confirm(
                f"Cancel all tasks in experiment {experiment_id}?", default=False
            )
        if not confirm:
            raise typer.Abort()

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        try:
            if task_id:
                response = client.post(f"{api_url}/tasks/{task_id}/cancel")
            else:
                response = client.post(f"{api_url}/experiments/{experiment_id}/cancel")

            if response.status_code == 200:
                data = response.json()
                cancelled = data.get("cancelled", {})

                if task_id:
                    trials = cancelled.get("trials_cancelled", 0)
                    jobs = cancelled.get("jobs_cancelled", 0)
                    console.print(
                        f"[green]Task {task_id} cancelled[/green] "
                        f"({trials} trials, {jobs} jobs)"
                    )
                else:
                    tasks = cancelled.get("tasks_cancelled", 0)
                    trials = cancelled.get("trials_cancelled", 0)
                    jobs = cancelled.get("jobs_cancelled", 0)
                    console.print(
                        f"[green]Experiment {experiment_id} cancelled[/green] "
                        f"({tasks} tasks, {trials} trials, {jobs} jobs)"
                    )
                return

            if response.status_code == 404:
                target = f"Task {task_id}" if task_id else f"Experiment {experiment_id}"
                console.print(f"[red]{target} not found[/red]")
                raise typer.Exit(1)

            if response.status_code == 400:
                detail = response.json().get("detail", "Cannot cancel")
                console.print(f"[yellow]{detail}[/yellow]")
                raise typer.Exit(1)

            console.print(
                f"[red]Cancel failed:[/red] {response.status_code} - {response.text}"
            )
            raise typer.Exit(1)

        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)
