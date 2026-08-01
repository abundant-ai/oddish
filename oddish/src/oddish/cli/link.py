"""``oddish link`` — print canonical dashboard addresses.

Every dashboard view is addressable by URL, down to line ranges inside
file previews. This command mints those addresses from the terminal so
scripts and agents can cite exact locations (a trial, a file, lines
12-20 of a solution script) without knowing the URL grammar.
"""

from __future__ import annotations

from typing import Optional

import typer

from oddish.cli.config import error_console, get_dashboard_url
from oddish.links import experiment_url, task_url, trial_task_id

link_app = typer.Typer(
    help="Print canonical dashboard URLs for tasks and trials.",
    no_args_is_help=True,
)

_BASE_OPTION = typer.Option(
    None,
    "--base-url",
    help="Dashboard base URL (defaults to ODDISH_DASHBOARD_URL or the public dashboard).",
)


def _fail(message: str) -> None:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(1)


@link_app.command("task")
def link_task(
    task_id: str = typer.Argument(..., help="Task id."),
    version: Optional[str] = typer.Option(
        None, help="Task version to pin: a number (3) or a full id (task-v3)."
    ),
    task_file: Optional[str] = typer.Option(
        None, "--file", help="Task-definition file to open."
    ),
    task_lines: Optional[str] = typer.Option(
        None, "--lines", help="Line range in the file, e.g. 12 or 12-20."
    ),
    base_url: Optional[str] = _BASE_OPTION,
) -> None:
    """Address a task page, optionally down to a file and line range."""
    try:
        url = task_url(
            task_id,
            base_url=base_url or get_dashboard_url(),
            version=version,
            task_file=task_file,
            task_lines=task_lines,
        )
    except ValueError as exc:
        _fail(str(exc))
        return
    typer.echo(url)


@link_app.command("trial")
def link_trial(
    trial_id: str = typer.Argument(..., help="Trial id (<task_id>-<n>)."),
    experiment: Optional[str] = typer.Option(
        None, help="Address on this experiment page instead of the task page."
    ),
    task: Optional[str] = typer.Option(
        None, help="Host task id (derived from the trial id when omitted)."
    ),
    tab: Optional[str] = typer.Option(
        None, help="Trial pane tab: summary|live|files|trajectory|artifacts."
    ),
    file: Optional[str] = typer.Option(
        None,
        help="Trial pane file to open (files tab unless --tab artifacts).",
    ),
    lines: Optional[str] = typer.Option(
        None, help="Line range in the trial file, e.g. 12 or 12-20."
    ),
    task_file: Optional[str] = typer.Option(
        None, help="Task-definition pane file to open beside the trial."
    ),
    task_lines: Optional[str] = typer.Option(
        None, help="Line range in the task-definition file."
    ),
    base_url: Optional[str] = _BASE_OPTION,
) -> None:
    """Address a trial, optionally down to files and line ranges."""
    resolved_base = base_url or get_dashboard_url()
    try:
        if experiment:
            url = experiment_url(
                experiment,
                base_url=resolved_base,
                task_id=task,
                trial_id=trial_id,
                tab=tab,
                file=file,
                lines=lines,
                task_file=task_file,
                task_lines=task_lines,
            )
        else:
            task_id = task or trial_task_id(trial_id)
            if not task_id:
                _fail(
                    f"cannot derive a task id from trial {trial_id!r}; "
                    "pass --task"
                )
                return
            url = task_url(
                task_id,
                base_url=resolved_base,
                trial_id=trial_id,
                tab=tab,
                file=file,
                lines=lines,
                task_file=task_file,
                task_lines=task_lines,
            )
    except ValueError as exc:
        _fail(str(exc))
        return
    typer.echo(url)
