from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    is_modal_api_url,
    require_api_key,
)

console = Console()


def delete(
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID to delete (or use --experiment)"),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment ID to delete (cannot be used with task_id)",
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
):
    """Delete a task or experiment.

    Examples:
        oddish delete <task_id>            # Delete a specific task
        oddish delete --experiment <id>    # Delete a specific experiment
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if task_id and experiment_id:
        console.print("[red]Provide either a task_id or --experiment, not both.[/red]")
        raise typer.Exit(1)

    if not task_id and not experiment_id:
        console.print("[yellow]Provide a task ID or --experiment to delete.[/yellow]")
        raise typer.Exit(1)

    if is_modal_api_url(api_url):
        console.print(
            "[yellow]Cleanup is not available for hosted Oddish instances.[/yellow]"
        )
        raise typer.Exit(1)

    if task_id:
        confirm = typer.confirm(f"Delete task {task_id} and its trials?", default=False)
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
