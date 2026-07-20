from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from oddish.cli.api import resolve_local_task_paths
from oddish.cli.config import console, error_console, print_json
from oddish.preflight.models import Finding, Severity
from oddish.preflight.runner import has_errors, run_checks


def _location(finding: Finding) -> str:
    if finding.path is None:
        return ""
    loc = str(finding.path)
    if finding.line is not None:
        loc = f"{loc}:{finding.line}"
    return f" [dim]{loc}[/dim]"


def render_findings(findings: list[Finding], *, downgrade: bool = False) -> None:
    """Print findings grouped by task, errors first.

    ``downgrade`` renders errors in warning colours — used by ``--force``, where
    the findings are informational because the run proceeds regardless.
    """
    by_task: dict[Path, list[Finding]] = {}
    for f in findings:
        by_task.setdefault(f.task_dir, []).append(f)

    for task_dir, items in by_task.items():
        error_console.print(f"\n[bold]{task_dir}[/bold]")
        ordered = sorted(items, key=lambda f: 0 if f.severity is Severity.ERROR else 1)
        for f in ordered:
            is_error = f.severity is Severity.ERROR and not downgrade
            colour = "red" if is_error else "yellow"
            label = f.severity.value if not downgrade else "forced"
            error_console.print(
                f"  [{colour}]{label}[/{colour}] "
                f"[dim]{f.check_id}[/dim] {f.message}{_location(f)}"
            )
            if f.fix_hint:
                error_console.print(f"        [dim]{f.fix_hint}[/dim]")


def gate_preflight(
    findings: list[Finding], *, force: bool, json_output: bool = False
) -> None:
    """Render findings and abort unless clean or forced.

    Shared by ``oddish preflight`` and ``oddish run``. Under ``--force`` the
    findings are still printed: skipping the gate must not mean skipping the
    information about what is being skipped.
    """
    failed = has_errors(findings)

    if json_output:
        print_json({"ok": not failed, "findings": [f.to_dict() for f in findings]})
    elif findings:
        render_findings(findings, downgrade=force and failed)

    if not failed:
        return

    if force:
        if not json_output:
            error_console.print(
                "\n[yellow]Preflight failed but --force was given; submitting anyway.[/yellow]"
            )
        return

    if not json_output:
        error_console.print(
            "\n[red]Preflight failed.[/red] Fix the errors above, or re-run with "
            "[bold]--force[/bold] to submit anyway."
        )
    raise typer.Exit(1)


def preflight(
    path: Annotated[
        Optional[Path],
        typer.Argument(help="Path to task or dataset directory"),
    ] = None,
    path_option: Annotated[
        Optional[Path],
        typer.Option("--path", help="Path to task or dataset directory"),
    ] = None,
    dataset: Annotated[
        Optional[str],
        typer.Option("--dataset", help="Harbor dataset name to resolve tasks from"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit findings as JSON for CI consumers."),
    ] = False,
) -> None:
    """Check local tasks for integrity problems before spending trials on them."""
    task_paths = resolve_local_task_paths(
        path=path,
        path_option=path_option,
        dataset=dataset,
        task_names=None,
        exclude_task_names=None,
        n_tasks=None,
        quiet=json_output,
    )

    findings = run_checks(task_paths)
    gate_preflight(findings, force=False, json_output=json_output)

    if not json_output:
        console.print(
            f"\n[green]Preflight passed[/green] "
            f"[dim]({len(task_paths)} task(s))[/dim]"
        )
