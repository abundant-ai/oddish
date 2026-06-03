from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()


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
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
) -> None:
    """List uploaded tasks."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    params: dict[str, int | str] = {"limit": limit, "offset": offset}
    if query:
        params["query"] = query

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

    for task in tasks:
        current_version = task.get("current_version")
        version = f"v{current_version}" if current_version is not None else "-"
        table.add_row(
            task.get("id", "-"),
            task.get("name") or "-",
            version,
            _format_trials(task),
            _format_reward(task),
            _format_datetime(task.get("last_run_at")),
            _format_experiments(task),
        )

    console.print(table)
    if result.get("has_more"):
        next_offset = offset + limit
        console.print(f"[dim]More available: oddish ls --offset {next_offset}[/dim]")
