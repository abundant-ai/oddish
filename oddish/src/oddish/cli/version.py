"""Print the installed Oddish CLI version."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from oddish.cli._package import (
    PackageError,
    fetch_pypi_latest,
    inspect_install,
    is_outdated,
)
from oddish.cli.config import error_console


def show_version_flag(value: bool) -> None:
    """Eager ``oddish --version`` hook."""
    if value:
        typer.echo(f"oddish {inspect_install().version}")
        raise typer.Exit()


def version_cmd(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Compare the installed version with the latest PyPI release.",
        ),
    ] = False,
) -> None:
    """Print the installed CLI version. No API key required."""
    info = inspect_install()
    payload = info.as_dict()
    outdated = False
    latest: str | None = None
    if check:
        try:
            latest = fetch_pypi_latest()
        except PackageError as exc:
            error_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc
        pypi_install = info.source == "pypi"
        outdated = pypi_install and is_outdated(info.version, latest)
        payload["latest"] = latest
        payload["update_available"] = outdated if pypi_install else None

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"oddish {info.version}")
        typer.echo(f"Source: {info.source_label} ({info.manager_label})")
        if check and latest is not None:
            if info.source != "pypi":
                typer.echo(f"Latest PyPI: {latest} (this install is not from PyPI)")
            elif outdated:
                typer.echo(f"Latest: {latest} (update available)")
            else:
                typer.echo(f"Latest: {latest} (up to date)")

    if check and outdated:
        raise typer.Exit(1)
