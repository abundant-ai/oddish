from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()


def _normalize_sources(raw: list[str]) -> list[str]:
    """Strip, drop blanks, and dedupe source ids while preserving order.

    Pure (no I/O) so it can be unit-tested. The server enforces the
    "at least two distinct" rule too; this mirrors it client-side for a
    fast, friendly error.
    """
    return list(dict.fromkeys(stripped for s in raw if s and (stripped := s.strip())))


def _format_combine_summary(data: dict) -> list[str]:
    """Build human-readable summary lines for a combine result.

    Pure (no I/O) so it can be unit-tested directly.
    """
    sources = data.get("source_experiment_ids") or []
    lines = [
        f"[green]Created experiment {data.get('id')}[/green] ({data.get('name')})",
        f"  Sources combined: {len(sources)}",
        f"  Tasks linked:     {data.get('tasks_linked', 0)}",
        f"  Trials copied:    {data.get('trials_copied', 0)}",
    ]
    skipped = data.get("trials_skipped", 0)
    if skipped:
        lines.append(
            f"  Trials skipped:   {skipped} [dim](not finished at combine time)[/dim]"
        )
    lines.append(f"  Artifacts copied: {data.get('artifacts_copied', 0)}")
    return lines


def combine(
    source_experiment_ids: Annotated[
        list[str],
        typer.Argument(
            help="Two or more experiment IDs or names to combine.",
        ),
    ],
    name: Annotated[
        Optional[str],
        typer.Option(
            "--name",
            "-n",
            help="Name for the result experiment (auto-generated if omitted).",
        ),
    ] = None,
    copy_artifacts: Annotated[
        bool,
        typer.Option(
            "--copy-artifacts/--no-copy-artifacts",
            help=(
                "Duplicate each copied trial's artifacts so the result is fully "
                "independent (default), or reference the source artifacts in "
                "place (cheaper, shared storage)."
            ),
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the raw JSON response."),
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option(
            "--api-url",
            "-u",
            help="API URL (uses configured URL if not specified).",
        ),
    ] = None,
):
    """Combine several experiments into a new result experiment.

    Creates a brand-new experiment and copies the task memberships and
    finished trials (with their artifacts) of every source experiment
    into it. The source experiments are left untouched.

    Examples:
        oddish combine <exp_a> <exp_b>
        oddish combine <exp_a> <exp_b> <exp_c> --name nightly-rollup
        oddish combine <exp_a> <exp_b> --no-copy-artifacts
    """
    sources = _normalize_sources(source_experiment_ids)
    if len(sources) < 2:
        console.print(
            "[red]Provide at least two distinct experiments to combine.[/red]"
        )
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    payload: dict[str, object] = {
        "source_experiment_ids": sources,
        "copy_artifacts": copy_artifacts,
    }
    if name:
        payload["name"] = name

    # Copying artifacts can fan out to many server-side S3 copies, so allow
    # a generous timeout relative to the other (instant) management commands.
    with httpx.Client(timeout=300.0, headers=get_auth_headers()) as client:
        try:
            response = client.post(f"{api_url}/experiments/combine", json=payload)
        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)

    if response.status_code != 200:
        console.print(
            f"[red]Combine failed:[/red] {response.status_code} - {response.text}"
        )
        raise typer.Exit(1)

    data = response.json()

    if json_output:
        console.print_json(data=data)
        return

    for line in _format_combine_summary(data):
        console.print(line)

    exp_id = data.get("id")
    if exp_id:
        console.print(f"  View: {get_dashboard_url(api_url)}/experiments/{exp_id}")
