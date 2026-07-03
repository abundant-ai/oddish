"""``oddish probe`` — queue a probe trial against an existing task.

A probe is an ordinary sweep with ``extra_instructions`` set: the server
flags the trial ``mode: "probe"`` and the cloud worker applies the instruction
overlay before the agent runs (see ``queue._build_harbor_config_for_trial`` and
``workers/queue/trial_handler``). This command is the CLI equivalent of the
probe submit form in the UI, which posts the same fields to ``/tasks/sweep``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
import yaml
from rich.console import Console

from oddish.cli.api import submit_sweep, watch_task
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()

probe_app = typer.Typer(
    help="Queue probe trials against a task, and manage org skills.",
    no_args_is_help=False,
)


@probe_app.callback(invoke_without_command=True)
def probe(
    ctx: typer.Context,
    task_id: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            help="Existing task ID to queue the probe against.",
        ),
    ] = None,
    instructions: Annotated[
        Optional[str],
        typer.Option(
            "--instructions",
            "--extra-instructions",
            help="Probe instructions injected into the agent's instruction.md.",
        ),
    ] = None,
    result_focus: Annotated[
        Optional[str],
        typer.Option(
            "--result-focus",
            help="Optional focus hint for what the probe should report on.",
        ),
    ] = None,
    agent: Annotated[
        str,
        typer.Option("--agent", "-a", help="Agent to run the probe with."),
    ] = "claude-code",
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Model to use (optional)."),
    ] = None,
    n_trials: Annotated[
        int,
        typer.Option("--n-trials", help="Number of probe trials to queue."),
    ] = 1,
    priority: Annotated[
        str,
        typer.Option("--priority", "-P", help="Priority (low or high)."),
    ] = "low",
    user: Annotated[
        Optional[str],
        typer.Option("--user", "-u", help="Override the submitting identity."),
    ] = "cli-probe",
    watch: Annotated[
        bool,
        typer.Option(
            "--watch/--background",
            "-w",
            help="Watch the probe trial until completion (default: enabled).",
        ),
    ] = True,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL (defaults to ODDISH_API_URL)."),
    ] = "",
):
    """Queue a probe trial against an existing task.

    EXAMPLES:

        oddish probe --task task_123 --instructions "investigate test flakiness"
        oddish probe --task task_123 -i "find the slow query" --result-focus "root cause"
    """
    if ctx.invoked_subcommand is not None:
        return
    if not task_id:
        error_console.print("[red]--task is required to queue a probe.[/red]")
        raise typer.Exit(1)
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    # A probe with no instructions has nothing to inject into instruction.md.
    if not instructions or not instructions.strip():
        error_console.print(
            "[red]--instructions is required: a probe needs text to inject "
            "into the agent's instruction.md.[/red]"
        )
        raise typer.Exit(1)

    # Probes always attach to an existing task and inherit its environment, so
    # we hardcode append_to_task=True / environment=None and never create an
    # experiment here. extra_instructions is what flips the server into
    # mode: "probe" (queue._build_harbor_config_for_trial).
    result = submit_sweep(
        api_url=api_url,
        task_id=task_id,
        configs=[{"agent": agent, "model": model, "n_trials": n_trials}],
        environment=None,
        user=user,
        priority=priority,
        experiment_id=None,
        append_to_task=True,
        extra_instructions=instructions.strip(),
        result_focus=result_focus.strip() if result_focus else None,
    )

    new_trial_ids = result.get("new_trial_ids") or []
    dashboard_url = get_dashboard_url(api_url)
    console.print("[bold green]Probe queued![/bold green]")
    console.print(f"  Task ID:    {result['id']}")
    console.print(f"  New trials: {result.get('trials_count', len(new_trial_ids))}")
    for trial_id in new_trial_ids:
        console.print(
            f"  Probe:      {dashboard_url}/tasks/{result['id']}/probe/{trial_id}"
        )

    if watch and len(new_trial_ids) == 1:
        console.print("\n[dim]Watching probe (Ctrl+C to stop)...[/dim]\n")
        try:
            final = watch_task(api_url, result["id"], trial_ids=new_trial_ids)
            if final:
                console.print("[green]Probe complete.[/green]")
        except KeyboardInterrupt:
            console.print(
                f"\n[dim]Stopped watching. Resume: oddish status {result['id']} --watch[/dim]"
            )


_SKIP_DIRS = {".git", "__pycache__"}
_SKIP_FILES = {".DS_Store"}


def _collect_skill_files(directory: Path) -> list[dict]:
    """Walk ``directory`` into a list of ``{relative_path, content}`` entries,
    skipping VCS/cache junk. Paths are POSIX-style relative to the root."""
    files: list[dict] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(directory)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.name in _SKIP_FILES or path.suffix == ".pyc":
            continue
        files.append({"relative_path": rel.as_posix(), "content": path.read_text()})
    return files


def _parse_skill_meta(skill_md: str) -> tuple[str, str]:
    """Extract (name, description) from SKILL.md frontmatter for the request
    body. The server re-validates and is authoritative; this fails fast for a
    nicer local error."""
    text = skill_md.lstrip()
    parts = text.split("---", 2)
    if not text.startswith("---") or len(parts) < 3:
        error_console.print(
            "[red]SKILL.md must start with closed YAML frontmatter (---).[/red]"
        )
        raise typer.Exit(1)
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        error_console.print("[red]SKILL.md frontmatter is not valid YAML.[/red]")
        raise typer.Exit(1)
    name = meta.get("name") if isinstance(meta, dict) else None
    description = meta.get("description") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name:
        error_console.print("[red]SKILL.md frontmatter is missing 'name'.[/red]")
        raise typer.Exit(1)
    if not isinstance(description, str) or not description:
        error_console.print("[red]SKILL.md frontmatter is missing 'description'.[/red]")
        raise typer.Exit(1)
    return name, description


skill_app = typer.Typer(
    help="Manage org skills (auto-staged into every trial).",
    no_args_is_help=True,
)
probe_app.add_typer(skill_app, name="skill")


@skill_app.command("add")
def skill_add(
    directory: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Path to the skill folder (must contain a root SKILL.md).",
        ),
    ],
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL (defaults to ODDISH_API_URL)."),
    ] = "",
):
    """Upload a local skill folder to your org's skills DB.

    EXAMPLES:

        oddish probe skill add ./my-skill
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    files = _collect_skill_files(directory)
    skill_md = next((f for f in files if f["relative_path"] == "SKILL.md"), None)
    if skill_md is None:
        error_console.print("[red]No SKILL.md found in the skill directory root.[/red]")
        raise typer.Exit(1)
    name, description = _parse_skill_meta(skill_md["content"])

    payload = {"name": name, "description": description, "files": files}
    with httpx.Client(timeout=60.0, headers=get_auth_headers(api_url)) as client:
        response = client.post(f"{api_url}/skills", json=payload)
    if response.status_code != 200:
        error_console.print(f"[red]Failed to add skill:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()
    console.print(
        f"[bold green]Added skill[/bold green] '{result['name']}' ({result['id']})"
    )
