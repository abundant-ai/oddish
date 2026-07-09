"""Read-only detail views for ``oddish status``.

Wires the existing task/trial detail endpoints into the CLI so agents can inspect
a single trial, a task's version history, and per-version cost rollups without
direct DB access:

- ``oddish status <trial_id>``            -> ``GET /trials/{id}`` (falls back to
  the parent task's embedded trial on a self-hosted core server, which has no
  single-trial route)
- ``oddish status <task_id> --detail``    -> ``GET /tasks/{id}/detail``
- ``oddish status <task_id> --versions``  -> ``GET /tasks/{id}/versions``
- ``oddish status <task_id> --versions --version N`` -> one version
"""

from __future__ import annotations

from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_auth_headers, print_json

console = Console()
error_console = Console(stderr=True)

_TIMEOUT = 15.0


def _looks_like_trial_id(value: str) -> bool:
    """Trial ids are ``{task_id}-{index}`` with a numeric trailing segment."""
    head, sep, tail = value.rpartition("-")
    return bool(sep) and bool(head) and tail.isdigit()


def _fmt_reward(reward: Any) -> str:
    if reward is None:
        return "-"
    try:
        value = float(reward)
    except (TypeError, ValueError):
        return str(reward)
    if value == 1:
        return "[green]1 (pass)[/green]"
    if value == 0:
        return "[red]0 (fail)[/red]"
    return f"[yellow]{value:.2f} (partial)[/yellow]"


def _fmt_cost(trial: dict[str, Any]) -> str:
    cost = trial.get("cost_usd")
    if cost is None:
        return "-"
    tag = " (est)" if trial.get("cost_is_estimated") else ""
    return f"${float(cost):.4f}{tag}"


def _fmt_tokens(trial: dict[str, Any]) -> str:
    parts = []
    for label, key in (("in", "input_tokens"), ("cache", "cache_tokens"), ("out", "output_tokens")):
        value = trial.get(key)
        if value is not None:
            parts.append(f"{label} {int(value):,}")
    return ", ".join(parts) if parts else "-"


def _render_trial(trial: dict[str, Any]) -> None:
    console.print(f"[bold]Trial:[/bold] {trial.get('id', '-')}")
    console.print(f"[bold]Task:[/bold] {trial.get('task_id', '-')}")
    if trial.get("experiment_id"):
        console.print(f"[bold]Experiment:[/bold] {trial['experiment_id']}")
    console.print(
        f"[bold]Agent/Model:[/bold] {trial.get('agent', '-')} / "
        f"{trial.get('model') or '-'}"
    )
    console.print(f"[bold]Provider:[/bold] {trial.get('provider') or '-'}")
    console.print(f"[bold]Queue key:[/bold] {trial.get('queue_key') or '-'}")
    console.print(f"[bold]Environment:[/bold] {trial.get('environment') or '-'}")
    console.print(f"[bold]Status:[/bold] {trial.get('status', '-')}")
    console.print(f"[bold]Harbor stage:[/bold] {trial.get('harbor_stage') or '-'}")
    console.print(f"[bold]Reward:[/bold] {_fmt_reward(trial.get('reward'))}")
    console.print(
        f"[bold]Attempts:[/bold] {trial.get('attempts', 0)}/"
        f"{trial.get('max_attempts', '-')}"
    )
    console.print(f"[bold]Tokens:[/bold] {_fmt_tokens(trial)}")
    console.print(f"[bold]Steps:[/bold] {trial.get('total_steps') if trial.get('total_steps') is not None else '-'}")
    console.print(f"[bold]Cost:[/bold] {_fmt_cost(trial)}")
    console.print(
        f"[bold]Trajectory:[/bold] "
        f"{'yes' if trial.get('has_trajectory') else 'no'}"
    )

    analysis = trial.get("analysis")
    if isinstance(analysis, dict) and analysis:
        classification = analysis.get("classification") or "-"
        console.print(f"[bold]Analysis:[/bold] {classification}")
        recommendation = analysis.get("recommendation")
        if recommendation:
            console.print(f"  [dim]{recommendation}[/dim]")
    elif trial.get("analysis_status"):
        console.print(f"[bold]Analysis:[/bold] {trial['analysis_status']}")

    phase_timing = trial.get("phase_timing")
    if isinstance(phase_timing, dict) and phase_timing:
        parts = [
            f"{phase}={float(secs):.0f}s"
            for phase, secs in phase_timing.items()
            if isinstance(secs, (int, float))
        ]
        if parts:
            console.print(f"[bold]Phase timing:[/bold] {', '.join(parts)}")

    error = trial.get("error_message")
    if error:
        console.print(f"[bold red]Error:[/bold red] {error}")


def try_print_trial_detail(
    api_url: str,
    trial_id: str,
    *,
    json_output: bool,
) -> bool:
    """Render a single trial. Returns True if ``trial_id`` resolved to a trial.

    Tries the hosted single-trial route first; on a core server (no such route)
    it falls back to the parent task's embedded trial. Returns False if the id
    is not a trial (so the caller can fall through to task/experiment lookup).
    """
    if not _looks_like_trial_id(trial_id):
        return False

    headers = get_auth_headers(api_url)
    trial: dict[str, Any] | None = None
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        response = client.get(f"{api_url}/trials/{trial_id}")
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                trial = data
        elif response.status_code == 404:
            # A 404 means "no single-trial route" (self-hosted core server) or
            # "trial genuinely absent". Before treating the id as a trial of its
            # parent, check whether it is itself a task: a distinct task whose id
            # happens to match the `{parent}-{index}` shape must resolve to its
            # own task status, not get shadowed by parent-task-{index}'s trial.
            if client.get(f"{api_url}/tasks/{trial_id}").status_code == 200:
                return False  # it's a task; let normal status handling show it
            # Core-server fallback: fetch the trial by its index. This route
            # returns the exact trial by id -- including superseded and
            # non-current-version trials -- unlike scanning the parent task's
            # (current-version-only) embedded trial list, which would drop them.
            parent, _, index = trial_id.rpartition("-")
            idx_response = client.get(f"{api_url}/tasks/{parent}/trials/{index}")
            if idx_response.status_code == 200:
                data = idx_response.json()
                if isinstance(data, dict) and data.get("id") == trial_id:
                    trial = data
        else:
            # Genuine error on the single-trial route (auth, server error, ...).
            _fail(response, json_output, "Failed to get trial")

    if trial is None:
        return False

    if json_output:
        print_json(trial)
    else:
        _render_trial(trial)
    return True


def _fetch(api_url: str, path: str) -> httpx.Response:
    headers = get_auth_headers(api_url)
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        return client.get(f"{api_url}{path}")


def print_task_detail(api_url: str, task_id: str, *, json_output: bool) -> None:
    """Render ``GET /tasks/{id}/detail``: header, cost totals, per-version rollup."""
    response = _fetch(api_url, f"/tasks/{task_id}/detail")
    if response.status_code != 200:
        _fail(response, json_output, "Failed to get task detail")

    data = response.json()
    if json_output:
        print_json(data)
        return

    task = data.get("task") or {}
    totals = data.get("totals") or {}
    versions = data.get("versions") or []

    console.print(f"[bold]Task:[/bold] {task.get('id', task_id)}")
    console.print(f"[bold]Name:[/bold] {task.get('name') or '-'}")
    console.print(f"[bold]Status:[/bold] {task.get('status', '-')}")
    if task.get("progress"):
        console.print(f"[bold]Progress:[/bold] {task['progress']}")

    cost = totals.get("cost_usd")
    if cost is not None:
        est = " (incl. estimated)" if totals.get("cost_has_estimated") else ""
        console.print(
            f"[bold]Total cost:[/bold] ${float(cost):.4f} across "
            f"{totals.get('cost_trial_count', 0)} trials{est}"
        )
        # Gate on the billed trial count, not the amount, so a legitimate
        # $0.00 across N billed trials still shows (a zero amount is real
        # accounting, not "nothing billed").
        if totals.get("billed_trial_count"):
            billed = totals.get("billed_cost_usd") or 0.0
            console.print(
                f"[bold]Billed cost:[/bold] ${float(billed):.4f} across "
                f"{totals.get('billed_trial_count', 0)} trials"
            )

    if versions:
        table = Table(title="Versions", show_header=True)
        table.add_column("Ver", justify="right", style="cyan")
        table.add_column("Cur")
        table.add_column("Trials", justify="right")
        table.add_column("Pass/Total")
        table.add_column("Cost", justify="right")
        table.add_column("Message")
        for version in versions:
            reward_total = int(version.get("reward_total") or 0)
            passes = int(version.get("pass_count") or 0)
            reward_str = f"{passes}/{reward_total}" if reward_total else "-"
            vcost = version.get("cost_usd")
            # Show a real $0.0000 (a version with only free/zero-cost trials);
            # only "-" when cost is genuinely absent.
            cost_str = f"${float(vcost):.4f}" if vcost is not None else "-"
            table.add_row(
                str(version.get("version", "-")),
                "*" if version.get("is_current") else "",
                str(version.get("trial_count", 0)),
                reward_str,
                cost_str,
                (version.get("message") or "-")[:50],
            )
        console.print(table)


def print_task_versions(
    api_url: str,
    task_id: str,
    *,
    version: int | None,
    json_output: bool,
) -> None:
    """Render ``GET /tasks/{id}/versions`` (list) or one version."""
    path = (
        f"/tasks/{task_id}/versions/{version}"
        if version is not None
        else f"/tasks/{task_id}/versions"
    )
    response = _fetch(api_url, path)
    if response.status_code != 200:
        _fail(response, json_output, "Failed to get task versions")

    data = response.json()
    if json_output:
        print_json(data)
        return

    if version is not None:
        _render_version_fields(data)
        return

    versions = data if isinstance(data, list) else []
    if not versions:
        console.print("[dim]No versions found[/dim]")
        return
    table = Table(title=f"Versions of {task_id}", show_header=True)
    table.add_column("Ver", justify="right", style="cyan")
    table.add_column("Created")
    table.add_column("Content hash")
    table.add_column("Message")
    for row in versions:
        content_hash = row.get("content_hash") or "-"
        table.add_row(
            str(row.get("version", "-")),
            str(row.get("created_at") or "-"),
            content_hash[:12],
            (row.get("message") or "-")[:50],
        )
    console.print(table)


def _render_version_fields(data: dict[str, Any]) -> None:
    console.print(f"[bold]Version:[/bold] {data.get('version', '-')}")
    console.print(f"[bold]Task:[/bold] {data.get('task_id', '-')}")
    console.print(f"[bold]Content hash:[/bold] {data.get('content_hash') or '-'}")
    console.print(f"[bold]Task path:[/bold] {data.get('task_path') or '-'}")
    console.print(f"[bold]S3 key:[/bold] {data.get('task_s3_key') or '-'}")
    console.print(f"[bold]Created:[/bold] {data.get('created_at') or '-'}")
    if data.get("message"):
        console.print(f"[bold]Message:[/bold] {data['message']}")


def _fail(response: httpx.Response, json_output: bool, message: str) -> None:
    if json_output:
        print_json({"error": response.text, "status": response.status_code})
    else:
        error_console.print(f"[red]{message}:[/red] {response.text}")
    raise typer.Exit(1)
