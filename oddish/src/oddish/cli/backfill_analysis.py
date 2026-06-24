from __future__ import annotations

from typing import Annotated, Optional

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


def _task_id_from_trial(trial_id: str) -> str | None:
    """`<task_id>-<index>` -> `<task_id>` (mirrors cancel._split_trial_id)."""
    task_id, sep, index = trial_id.rpartition("-")
    if not sep or not index.isdigit():
        return None
    return task_id or None


def backfill_analysis(
    experiment: Annotated[
        Optional[str], typer.Option("--experiment", help="Backfill every task in this experiment")
    ] = None,
    task: Annotated[
        Optional[str], typer.Option("--task", help="Backfill this task")
    ] = None,
    trial: Annotated[
        Optional[str], typer.Option("--trial", help="Backfill this trial (runs as its task's QA job)")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-run analysis even for already-analyzed trials")
    ] = False,
    enable_analysis: Annotated[
        bool,
        typer.Option("--enable-analysis", help="Also set run_analysis=true so future trials auto-analyze"),
    ] = False,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Output JSON (for CI/scripts)")
    ] = False,
):
    """(Re)run trial analysis for an experiment, a task, or a single trial.

    Examples:
        oddish backfill-analysis --task <task_id>
        oddish backfill-analysis --trial <trial_id> --force
        oddish backfill-analysis --experiment <experiment_id> --enable-analysis
    """
    chosen = [(name, val) for name, val in
              (("--experiment", experiment), ("--task", task), ("--trial", trial)) if val]
    if len(chosen) != 1:
        console.print("[red]Provide exactly one of --experiment, --task, or --trial[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    # Resolve scope -> list of (task_id, trial_ids) jobs.
    jobs: list[tuple[str, list[str] | None]] = []
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        if trial:
            resolved = _task_id_from_trial(trial)
            if not resolved:
                console.print(f"[red]'{trial}' is not a valid trial id (expected <task_id>-<index>)[/red]")
                raise typer.Exit(1)
            jobs.append((resolved, [trial]))
        elif task:
            jobs.append((task, None))
        else:  # experiment
            resp = client.get(f"{api_url}/tasks", params={"experiment_id": experiment})
            if resp.status_code != 200:
                console.print(f"[red]Failed to list tasks for experiment {experiment}:[/red] {resp.text}")
                raise typer.Exit(1)
            task_rows = resp.json()
            if not task_rows:
                console.print(f"[yellow]No tasks found for experiment {experiment}[/yellow]")
                raise typer.Exit(1)
            jobs = [(row["id"], None) for row in task_rows]

        results: list[dict] = []
        queued = skipped = 0
        for task_id, trial_ids in jobs:
            resp = client.post(
                f"{api_url}/tasks/{task_id}/qa/backfill",
                json={"force": force, "enable_analysis": enable_analysis, "trial_ids": trial_ids},
            )
            if resp.status_code == 200:
                body = resp.json()
                results.append({"task_id": task_id, **body})
                queued += 1
                if not json_output:
                    console.print(
                        f"[green]queued[/green] {task_id} "
                        f"(trials={body.get('trial_count', 0)}, reset={body.get('reset_count', 0)})"
                    )
            else:
                results.append({"task_id": task_id, "error": resp.text, "status": resp.status_code})
                skipped += 1
                if not json_output:
                    console.print(f"[yellow]skipped[/yellow] {task_id}: {resp.text}")

    if json_output:
        print_json({"queued": queued, "skipped": skipped, "results": results})
    else:
        console.print(f"\n[bold]Backfill: queued {queued}, skipped {skipped}[/bold]")

    if queued == 0:
        raise typer.Exit(1)
