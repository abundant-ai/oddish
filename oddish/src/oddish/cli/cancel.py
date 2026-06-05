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
from oddish.cli.api import get_task_summary

console = Console()


def _split_trial_id(value: str) -> str | None:
    task_id, sep, maybe_index = value.rpartition("-")
    if not sep or not maybe_index.isdigit():
        return None
    return task_id or None


def _resolve_analysis_target(api_url: str, target_id: str) -> tuple[str, str]:
    parent_task_id = _split_trial_id(target_id)
    if parent_task_id:
        task = get_task_summary(api_url, parent_task_id)
        if task and any(t.get("id") == target_id for t in task.get("trials", []) or []):
            return "trial", target_id
    return "task", target_id


def _request_cancel(api_url: str, path: str, *, task_id: str | None = None):
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        if task_id is None:
            return client.post(f"{api_url}{path}")
        return client.post(
            f"{api_url}{path}",
            json={"task_ids": [task_id]},
        )


def cancel(
    task_id: Annotated[
        str,
        typer.Argument(help="Task or trial ID to cancel"),
    ],
    analysis: Annotated[
        bool,
        typer.Option(
            "--analysis",
            help=(
                "Cancel active analysis only. A trial-shaped ID cancels one "
                "trial analysis; otherwise cancels task analysis."
            ),
        ),
    ] = False,
    verdict: Annotated[
        bool,
        typer.Option(
            "--verdict",
            help="Cancel the active task verdict only.",
        ),
    ] = False,
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
    """Cancel in-flight runs or pipeline jobs.

    Stops running trials, cancels queued jobs, and terminates Modal workers.
    Task data and completed trial results are preserved.

    Examples:
        oddish cancel <task_id>
        oddish cancel <task_id> --analysis
        oddish cancel <task_id> --verdict
        oddish cancel <trial_id> --analysis
        oddish cancel <task_id> --force
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if analysis and verdict:
        message = "Use only one of --analysis or --verdict."
        if json_output:
            print_json({"error": message})
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(1)

    action_label = "all runs"
    path = "/tasks/cancel"
    request_task_id: str | None = task_id
    target_label = f"task {task_id}"
    if analysis:
        target_type, target_id = _resolve_analysis_target(api_url, task_id)
        if target_type == "trial":
            path = f"/trials/{target_id}/analysis/cancel"
            request_task_id = None
            target_label = f"trial {target_id}"
        else:
            path = f"/tasks/{target_id}/analysis/cancel"
            request_task_id = None
            target_label = f"task {target_id}"
        action_label = "analysis"
    elif verdict:
        target_id = _split_trial_id(task_id) or task_id
        path = f"/tasks/{target_id}/verdict/cancel"
        request_task_id = None
        target_label = f"task {target_id}"
        action_label = "verdict"

    if not force and not json_output:
        confirm = typer.confirm(f"Cancel {action_label} for {target_label}?")
        if not confirm:
            console.print("[dim]Aborted[/dim]")
            raise typer.Exit(0)

    response = _request_cancel(api_url, path, task_id=request_task_id)

    if response.status_code == 404:
        if json_output:
            print_json({"error": f"{target_label.capitalize()} not found", "status": 404})
        else:
            console.print(f"[red]{target_label.capitalize()} not found[/red]")
        raise typer.Exit(1)

    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            console.print(f"[red]Failed to cancel {action_label}:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()

    if json_output:
        print_json({"task_id": task_id, **result})
        return
    if analysis:
        console.print(f"[green]Cancelled analysis for {target_label}[/green]")
        jobs = result.get("analysis_jobs_cancelled", 0)
        trials = result.get("trials_cancelled", 0)
        if jobs:
            console.print(f"  Analysis jobs cancelled: {jobs}")
        if trials:
            console.print(f"  Trial analyses marked cancelled: {trials}")
        if not jobs and not trials:
            console.print("  [dim]No active analysis found[/dim]")
        return
    if verdict:
        console.print(f"[green]Cancelled verdict for {target_label}[/green]")
        jobs = result.get("verdict_jobs_cancelled", 0)
        if jobs:
            console.print(f"  Verdict jobs cancelled: {jobs}")
        if not jobs:
            console.print("  [dim]No active verdict found[/dim]")
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
