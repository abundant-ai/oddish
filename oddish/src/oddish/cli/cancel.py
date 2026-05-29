from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    print_json,
    require_api_key,
)

console = Console()


def cancel(
    task_id: Annotated[
        str,
        typer.Argument(help="Task ID to cancel"),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Skip confirmation prompt",
        ),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output JSON (for CI/scripts). Implies --force.",
        ),
    ] = False,
):
    """Cancel all in-flight runs for a task.

    Stops running trials, cancels queued jobs, and terminates Modal workers.
    Task data and completed trial results are preserved.

    Examples:
        oddish cancel <task_id>
        oddish cancel <task_id> --force
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if not force and not json_output:
        confirm = typer.confirm(f"Cancel all runs for task {task_id}?")
        if not confirm:
            console.print("[dim]Aborted[/dim]")
            raise typer.Exit(0)

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        response = client.post(
            f"{api_url}/tasks/cancel",
            json={"task_ids": [task_id]},
        )

    if response.status_code == 404:
        if json_output:
            print_json({"error": f"Task {task_id} not found", "status": 404})
        else:
            console.print(f"[red]Task {task_id} not found[/red]")
        raise typer.Exit(1)

    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            console.print(f"[red]Failed to cancel task:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()

    if json_output:
        print_json({"task_id": task_id, **result})
        return
    trials = result.get("trials_cancelled", 0)
    pgq = 0  # Legacy field, no longer tracked
    modal = result.get("modal_calls_cancelled", 0)

    console.print(f"[green]Cancelled task {task_id}[/green]")
    if trials:
        console.print(f"  Trials stopped: {trials}")
    if pgq:
        console.print(f"  Queue jobs cancelled: {pgq}")
    if modal:
        console.print(f"  Modal workers terminated: {modal}")
    if not trials and not pgq:
        console.print("  [dim]No active runs found[/dim]")
