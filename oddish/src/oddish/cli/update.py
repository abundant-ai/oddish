"""Upgrade a PyPI Oddish CLI install with ``uv pip install --upgrade oddish``."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Annotated, NoReturn

import typer

from oddish.cli._package import (
    InstallInfo,
    PackageError,
    fetch_pypi_latest,
    inspect_install,
    is_outdated,
    query_installed_version,
    upgrade_command,
)
from oddish.cli.config import console, error_console


def update_cmd(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Report whether a newer PyPI release is available without installing.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the upgrade command without running it.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reinstall even when the PyPI version already matches.",
        ),
    ] = False,
) -> None:
    """Upgrade this CLI from PyPI (the `uv pip install oddish` path).

    Compares the installed package with the latest PyPI release, then runs
    `uv pip install --upgrade oddish` into this interpreter (or `python -m pip`
    if uv is not on PATH). Editable and git installs are refused. No API key
    required.
    """
    info = inspect_install()
    try:
        command = upgrade_command(info)
    except PackageError as exc:
        _fail(str(exc), json_output=json_output, info=info)

    try:
        latest = fetch_pypi_latest()
    except PackageError as exc:
        if not force:
            _fail(str(exc), json_output=json_output, info=info, command=command)
        latest = None
    already_latest = latest is not None and not is_outdated(info.version, latest)

    payload = {
        **info.as_dict(),
        "command": command,
        "latest": latest,
        "update_available": None if latest is None else not already_latest,
    }

    if check:
        payload["action"] = "check"
        _emit(payload, json_output=json_output, info=info, latest=latest)
        if latest is not None and not already_latest:
            raise typer.Exit(1)
        return

    if already_latest and not force:
        payload["action"] = "already_latest"
        _emit(payload, json_output=json_output, info=info, latest=latest)
        return

    if dry_run:
        payload["action"] = "dry_run"
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(subprocess.list2cmdline(command))
        return

    if not json_output:
        if latest:
            console.print(f"Updating oddish {info.version} → {latest} via uv pip")
        else:
            console.print(f"Updating oddish {info.version} via uv pip")

    completed = subprocess.run(
        command,
        check=False,
        stdout=sys.stderr if json_output else None,
    )
    if completed.returncode != 0:
        _fail(
            f"Upgrade command exited {completed.returncode}.",
            json_output=json_output,
            info=info,
            command=command,
            code=completed.returncode,
        )

    try:
        new_version = query_installed_version(sys.executable)
    except PackageError:
        new_version = None

    payload["action"] = "updated"
    payload["new_version"] = new_version
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    if new_version:
        console.print(f"[green]Updated[/green] to oddish {new_version}")
    else:
        console.print("[green]Update finished[/green]")


def _emit(
    payload: dict[str, object],
    *,
    json_output: bool,
    info: InstallInfo,
    latest: str | None,
) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"oddish {info.version}")
    typer.echo(f"Source: {info.source_label} ({info.manager_label})")
    if latest is None:
        return
    if payload.get("action") == "already_latest":
        typer.echo(f"Latest: {latest} (already up to date)")
    elif is_outdated(info.version, latest):
        typer.echo(f"Latest: {latest} (update available)")
    else:
        typer.echo(f"Latest: {latest}")


def _fail(
    message: str,
    *,
    json_output: bool,
    info: InstallInfo,
    command: list[str] | None = None,
    code: int = 1,
) -> NoReturn:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    **info.as_dict(),
                    "action": "error",
                    "error": message,
                    "command": command,
                },
                indent=2,
            )
        )
    else:
        error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)
