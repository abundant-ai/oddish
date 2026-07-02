from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
experiment_app = typer.Typer(help="Manage experiments.", no_args_is_help=True)


def _normalize_trial_ids(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


def _format_collection_summary(data: dict) -> list[str]:
    return [
        f"[green]Created collection {data.get('id')}[/green] ({data.get('name')})",
        f"  Trials linked: {data.get('trials_linked', 0)}",
        f"  Tasks linked:  {data.get('tasks_linked', 0)}",
    ]


@experiment_app.command("create")
def create(
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to gather (optional; combine with --experiment)."),
    ] = None,
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Name for the collection experiment."),
    ] = ...,
    experiments: Annotated[
        Optional[list[str]],
        typer.Option("--experiment", "-e", help="Experiment id/name; links its qualifying trials. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw JSON response.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Gather existing trials into a new read-only collection experiment.

    The trials keep their home experiment; the collection just references
    them for viewing in the dashboard.

        oddish experiment create --name "my collection" trial_a trial_b trial_c
    """
    ids = _normalize_trial_ids(trial_ids or [])
    exp_ids = _normalize_trial_ids(experiments or [])
    if not ids and not exp_ids:
        console.print("[red]Provide at least one trial id or --experiment.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        resp = client.post(
            f"{api_url}/experiments/collections",
            json={"name": name, "trial_ids": ids, "experiment_ids": exp_ids},
        )
    if resp.status_code != 200:
        console.print(f"[red]Failed:[/red] {resp.text}")
        raise typer.Exit(1)

    data = resp.json()
    if json_output:
        import json as _json

        console.print_json(_json.dumps(data))
        return
    for line in _format_collection_summary(data):
        console.print(line)
