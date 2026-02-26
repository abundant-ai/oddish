from __future__ import annotations

import os
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    API_PID_PATH,
    get_api_url,
    get_auth_headers,
    is_local_api_url,
    is_modal_api_url,
)
from oddish.cli.infra import pid_is_running, read_api_pid
from oddish.infra import reset_postgres, stop_postgres

console = Console()


def clean(
    task_id: Annotated[
        Optional[str],
        typer.Argument(
            help="Task ID to delete (omit for infra cleanup or use --experiment)"
        ),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment ID to delete (cannot be used with task_id)",
        ),
    ] = None,
    stop_only: Annotated[
        bool,
        typer.Option(
            "--stop-only",
            help="Just stop infrastructure without deleting data (local only)",
        ),
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-u",
            help="API URL (uses configured URL if not specified)",
        ),
    ] = None,
):
    """Stop Oddish infrastructure and optionally delete data.

    For local/self-hosted: stops Docker containers and deletes data.

    Examples:
        oddish clean                      # Delete all data (local or cloud)
        oddish clean --stop-only          # Stop services, keep data (local only)
        oddish clean <task_id>            # Delete a specific task
        oddish clean --experiment <id>    # Delete a specific experiment
    """
    if not api_url:
        api_url = get_api_url()

    is_local = is_local_api_url(api_url)

    if task_id and experiment_id:
        console.print("[red]Provide either a task_id or --experiment, not both.[/red]")
        raise typer.Exit(1)
    delete_mode = task_id or experiment_id
    if delete_mode:
        if stop_only:
            console.print("[yellow]--stop-only is not applicable for deletes[/yellow]")
            raise typer.Exit(0)
        if is_modal_api_url(api_url):
            console.print(
                "[yellow]Cleanup is not available for hosted Oddish instances.[/yellow]"
            )
            raise typer.Exit(1)

        if task_id:
            confirm = typer.confirm(
                f"Delete task {task_id} and its trials?", default=False
            )
            if not confirm:
                raise typer.Abort()
        elif experiment_id:
            confirm = typer.confirm(
                f"Delete experiment {experiment_id} and all its tasks?", default=False
            )
            if not confirm:
                raise typer.Abort()
        with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
            try:
                if task_id:
                    response = client.delete(f"{api_url}/tasks/{task_id}")
                elif experiment_id:
                    response = client.delete(f"{api_url}/experiments/{experiment_id}")

                if response.status_code == 200:
                    data = response.json()
                    message = data.get("message") or "Delete successful"
                    console.print(f"[green]{message}[/green]")
                    return

                console.print(
                    f"[red]Delete failed:[/red] {response.status_code} - {response.text}"
                )
                raise typer.Exit(1)
            except httpx.RequestError as e:
                console.print(f"[red]Failed to connect to API:[/red] {e}")
                raise typer.Exit(1)

    if is_local:
        # Local/self-hosted: stop infrastructure
        api_pid = read_api_pid()
        if api_pid and pid_is_running(api_pid):
            try:
                os.kill(api_pid, 15)  # SIGTERM
                console.print("[green]API server stopped[/green]")
                API_PID_PATH.unlink(missing_ok=True)
            except Exception:
                pass

        if stop_only:
            # Just stop, don't delete data
            try:
                stop_postgres()
                console.print("[green]Infrastructure stopped (data preserved)[/green]")
            except RuntimeError as e:
                console.print(f"[red]Failed to stop:[/red] {e}")
                raise typer.Exit(1)
        else:
            # Stop and delete data
            confirm = typer.confirm(
                "This will delete all data in the local database. Continue?",
                default=False,
            )
            if not confirm:
                raise typer.Abort()

            try:
                reset_postgres()
                console.print("[green]Infrastructure stopped and data deleted[/green]")
            except RuntimeError as e:
                console.print(f"[red]Failed to reset:[/red] {e}")
                raise typer.Exit(1)
    else:
        if stop_only:
            console.print(
                "[yellow]--stop-only is not applicable for hosted mode[/yellow]"
            )
            raise typer.Exit(0)

        if is_modal_api_url(api_url):
            console.print(
                "[yellow]Cleanup is not available for hosted Oddish instances.[/yellow]"
            )
            raise typer.Exit(1)

        console.print(
            "[yellow]Provide a task ID or --experiment for API deletes.[/yellow]"
        )
        raise typer.Exit(1)
