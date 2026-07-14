from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
analyzer_app = typer.Typer(help="Manage cross-experiment analyzers.", no_args_is_help=True)


def _normalize_ids(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


@analyzer_app.command("create")
def create(
    experiment_ids: Annotated[
        list[str],
        typer.Option("--experiment", "-e", help="Experiment ID to include (repeatable)."),
    ] = [],
    name: Annotated[
        str, typer.Option("--name", "-n", help="Name for the analyzer.")
    ] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw JSON response.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Create a analyzer analyzing trajectories across experiments.

        oddish analyzer create -e exp_a -e exp_b --name "Q3 sweep"
    """
    ids = _normalize_ids(experiment_ids)
    if not ids:
        console.print("[red]Provide at least one experiment id with -e/--experiment.[/red]")
        raise typer.Exit(1)
    if not name.strip():
        console.print("[red]Provide a analyzer name with -n/--name.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        resp = client.post(
            f"{api_url}/analyzers", json={"name": name, "experiment_ids": ids}
        )
    if resp.status_code != 200:
        console.print(f"[red]Failed:[/red] {resp.text}")
        raise typer.Exit(1)

    data = resp.json()
    if json_output:
        import json as _json

        console.print_json(_json.dumps(data))
        return
    console.print(
        f"[green]Created analyzer {data.get('id')}[/green] ({data.get('name')}) "
        f"— status: {data.get('status')}"
    )
