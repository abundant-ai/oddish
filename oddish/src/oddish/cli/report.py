from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
report_app = typer.Typer(
    help="Manage cross-experiment reports.", no_args_is_help=True
)


def _normalize_ids(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


@report_app.command("create")
def create(
    experiment_ids: Annotated[
        list[str],
        typer.Option("--experiment", "-e", help="Experiment ID to include (repeatable)."),
    ] = [],
    name: Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            help="Optional name. Defaults to report_<N>_<experiment>.",
        ),
    ] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw JSON response.")
    ] = False,
    save_trials: Annotated[
        bool,
        typer.Option(
            "--save-trials",
            help="Also save each trial-level analysis to S3 (one JSON per job).",
        ),
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Create a report analyzing trajectories across experiments.

        oddish report create -e exp_a -e exp_b
        oddish report create -e exp_a --name "Q3 sweep"
    """
    ids = _normalize_ids(experiment_ids)
    if not ids:
        console.print("[red]Provide at least one experiment id with -e/--experiment.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    # Omit name entirely when unset so the server auto-generates it.
    payload: dict = {"experiment_ids": ids, "save_trial_analyses": save_trials}
    if name.strip():
        payload["name"] = name.strip()

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        resp = client.post(f"{api_url}/reports", json=payload)
    if resp.status_code != 200:
        console.print(f"[red]Failed:[/red] {resp.text}")
        raise typer.Exit(1)

    data = resp.json()
    if json_output:
        import json as _json

        console.print_json(_json.dumps(data))
        return
    console.print(
        f"[green]Created report {data.get('id')}[/green] ({data.get('name')}) "
        f"— status: {data.get('status')}"
    )
