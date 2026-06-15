"""``oddish probe`` — queue a probe trial against an existing task.

A probe is an ordinary sweep with ``extra_instructions`` set: the server
flags the trial ``mode: "probe"`` and the cloud worker applies the instruction
overlay before the agent runs (see ``queue._build_harbor_config_for_trial`` and
``workers/queue/trial_handler``). This command is the CLI equivalent of the
probe submit form in the UI, which posts the same fields to ``/tasks/sweep``.

Phase 1 takes raw ``--instructions`` text. ``--preset <name>`` (resolve a stored
preset via ``GET /probe-presets`` and render its fields) is a planned follow-up.
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console

from oddish.cli.api import submit_sweep, watch_task
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_dashboard_url,
    require_api_key,
)

console = Console()


def probe(
    task_id: Annotated[
        str,
        typer.Option(
            "--task",
            help="Existing task ID to queue the probe against.",
        ),
    ],
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
