from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    print_json,
)

console = Console()
error_console = Console(stderr=True)


def _share_url(api_url: str, public_token: str | None) -> str | None:
    if not public_token:
        return None
    return f"{get_dashboard_url(api_url)}/share/{public_token}"


def publish(
    experiment_id: Annotated[
        str,
        typer.Argument(help="Experiment ID (or name) to publish."),
    ],
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON (for CI/scripts)."),
    ] = False,
):
    """Publish an experiment for public, read-only access.

    Returns the shareable public URL. Anyone with the link can view the
    experiment (trial analysis and verdicts stay hidden from public viewers).

    Examples:
        oddish publish my-experiment
        oddish publish my-experiment --json
    """
    if not api_url:
        api_url = get_api_url()

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        response = client.post(f"{api_url}/experiments/{experiment_id}/publish")

    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(
                f"[red]Failed to publish experiment:[/red] "
                f"{response.status_code} - {response.text}"
            )
        raise typer.Exit(1)

    data = response.json()
    share_url = _share_url(api_url, data.get("public_token"))

    if json_output:
        print_json(
            {
                "experiment": data.get("name", experiment_id),
                "is_public": bool(data.get("is_public")),
                "public_token": data.get("public_token"),
                "public_url": share_url,
            }
        )
        return

    console.print(f"[green]Published experiment {experiment_id}[/green]")
    if share_url:
        console.print(f"  Public URL: {share_url}")


def unpublish(
    experiment_id: Annotated[
        str,
        typer.Argument(help="Experiment ID (or name) to unpublish."),
    ],
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON (for CI/scripts)."),
    ] = False,
):
    """Unpublish an experiment so its public link stops working.

    Examples:
        oddish unpublish my-experiment
        oddish unpublish my-experiment --json
    """
    if not api_url:
        api_url = get_api_url()

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        response = client.post(f"{api_url}/experiments/{experiment_id}/unpublish")

    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(
                f"[red]Failed to unpublish experiment:[/red] "
                f"{response.status_code} - {response.text}"
            )
        raise typer.Exit(1)

    data = response.json()
    if json_output:
        print_json(
            {
                "experiment": data.get("name", experiment_id),
                "is_public": bool(data.get("is_public")),
            }
        )
        return

    console.print(f"[green]Unpublished experiment {experiment_id}[/green]")
