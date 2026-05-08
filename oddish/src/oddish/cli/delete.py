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
        typer.Argument(help="Task ID to delete (or use --experiment / --trial)"),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-e",
            help="Experiment ID to delete (cannot be used with task_id)",
        ),
    ] = None,
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Option(
            "--trial",
            "-t",
            help=(
                "Trial ID to delete. Pass multiple times to delete several "
                "trials in one command (admin-only; works against hosted "
                "Oddish)."
            ),
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip confirmation prompts.",
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
    """Delete a task, experiment, or one or more trials.

    Examples:
        oddish delete <task_id>                         # delete a task
        oddish delete --experiment <exp_id>             # delete an experiment
        oddish delete --trial <trial_id>                # delete a single trial
        oddish delete -t <id_a> -t <id_b> -t <id_c>     # delete several trials
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    selectors = sum(bool(x) for x in (task_id, experiment_id, trial_ids))
    if selectors == 0:
        console.print(
            "[yellow]Provide a task ID, --experiment, or one or more "
            "--trial IDs to delete.[/yellow]"
        )
        raise typer.Exit(1)
    if selectors > 1:
        console.print("[red]Pick exactly one of: task_id, --experiment, --trial.[/red]")
        raise typer.Exit(1)

    # Per-trial deletes are allowed against hosted Oddish; only the
    # whole-task / whole-experiment cleanup endpoints are gated off.
    if (task_id or experiment_id) and is_modal_api_url(api_url):
        console.print(
            "[yellow]Cleanup is not available for hosted Oddish instances.[/yellow]"
        )
        raise typer.Exit(1)

    if task_id and not yes:
        confirm = typer.confirm(f"Delete task {task_id} and its trials?", default=False)
        if not confirm:
            raise typer.Abort()
    elif experiment_id and not yes:
        confirm = typer.confirm(
            f"Delete experiment {experiment_id} and all its tasks?", default=False
        )
        if not confirm:
            raise typer.Abort()
    elif trial_ids and not yes:
        listing = ", ".join(trial_ids)
        confirm = typer.confirm(
            f"Delete {len(trial_ids)} trial(s) ({listing})?", default=False
        )
        if not confirm:
            raise typer.Abort()

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        try:
            if task_id:
                response = client.delete(f"{api_url}/tasks/{task_id}")
                _report_response(response)
                return
            if experiment_id:
                response = client.delete(f"{api_url}/experiments/{experiment_id}")
                _report_response(response)
                return
            assert trial_ids is not None
            failures: list[str] = []
            for tid in trial_ids:
                response = client.delete(f"{api_url}/trials/{tid}")
                if response.status_code == 200:
                    data = _safe_json(response)
                    s3_keys = (
                        data.get("s3_keys_deleted") if isinstance(data, dict) else None
                    )
                    extra = (
                        f" (s3 keys deleted: {s3_keys})" if s3_keys is not None else ""
                    )
                    console.print(f"[green]Deleted trial {tid}[/green]{extra}")
                else:
                    failures.append(
                        f"  {tid}: HTTP {response.status_code} - {response.text}"
                    )
                    console.print(
                        f"[red]Delete failed for trial {tid}:[/red] "
                        f"{response.status_code} - {response.text}"
                    )
            if failures:
                raise typer.Exit(1)
        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)


def _report_response(response: httpx.Response) -> None:
    if response.status_code == 200:
        data = _safe_json(response)
        message = (
            data.get("message")
            if isinstance(data, dict) and data.get("message")
            else "Delete successful"
        )
        console.print(f"[green]{message}[/green]")
        return
    console.print(f"[red]Delete failed:[/red] {response.status_code} - {response.text}")
    raise typer.Exit(1)


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None
