"""Queue & worker scheduler diagnostics for ``oddish status --queue``.

These wrap the ``/admin/*`` diagnostic endpoints (queue-health, queue-status,
slots, orphaned-state, worker-jobs) so an agent can debug "queued but not
running", stuck slots, and zombie/stale workers without direct Postgres access.

On hosted Oddish the ``/admin/*`` endpoints require a full-scope API key
(``require_admin`` accepts ``APIKeyScope.FULL``); a self-hosted core server
applies no auth. A ``read``/``tasks`` key gets a clear 403 hint instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_auth_headers, print_json

console = Console()
error_console = Console(stderr=True)

# The admin diagnostics we aggregate. ``worker-jobs`` is hosted-only (the core
# standalone server does not register it), so a 404 there is expected and not an
# error.
_ADMIN_ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("queue_health", "/admin/queue-health", {}),
    ("queue_status", "/admin/queue-status", {}),
    ("slots", "/admin/slots", {}),
    ("orphaned_state", "/admin/orphaned-state", {}),
    ("worker_jobs", "/admin/worker-jobs", {}),
]


def _age(value: str | None) -> str:
    """Render an ISO timestamp as a compact "Ns/Nm/Nh ago" age."""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 0:
        return "0s ago"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _fetch_admin(
    api_url: str,
    stale_after: int,
) -> tuple[dict[str, Any], int | None]:
    """Fetch every admin diagnostic. Returns (payload, first_auth_error_status).

    ``first_auth_error_status`` is set to 401/403 if any endpoint rejects the
    credentials, so the caller can print a single actionable hint. A 404 (e.g.
    worker-jobs on the core server) records ``None`` for that key without being
    treated as an auth failure.
    """
    result: dict[str, Any] = {}
    auth_error: int | None = None
    headers = get_auth_headers(api_url)
    with httpx.Client(timeout=15.0, headers=headers) as client:
        for key, path, params in _ADMIN_ENDPOINTS:
            merged = dict(params)
            if key in ("orphaned_state", "worker_jobs"):
                merged["stale_after_minutes"] = stale_after
            try:
                response = client.get(f"{api_url}{path}", params=merged)
            except httpx.HTTPError as exc:
                result[key] = {"error": str(exc)}
                continue
            if response.status_code == 200:
                result[key] = response.json()
            elif response.status_code == 404:
                # Endpoint not available on this deployment (e.g. worker-jobs on
                # the standalone core server). Not an error.
                result[key] = None
            elif response.status_code in (401, 403):
                auth_error = auth_error or response.status_code
                result[key] = {
                    "error": response.text,
                    "status": response.status_code,
                }
            else:
                result[key] = {
                    "error": response.text,
                    "status": response.status_code,
                }
    return result, auth_error


def _render_health(health: dict[str, Any]) -> None:
    console.print("[bold cyan]Queue health[/bold cyan]")
    console.print(
        f"  queued [yellow]{health.get('totals_queued', 0)}[/yellow]   "
        f"running [blue]{health.get('totals_running', 0)}[/blue]"
    )

    dispatcher = health.get("dispatcher")
    reconciler = health.get("reconciler")
    for label, comp in (("dispatcher", dispatcher), ("reconciler", reconciler)):
        if comp:
            console.print(
                f"  {label}: last seen [green]{_age(comp.get('updated_at'))}[/green]"
            )
        else:
            console.print(f"  {label}: [red]no heartbeat recorded[/red]")

    capacity = health.get("capacity") or []
    if capacity:
        table = Table(title="Capacity by queue", show_header=True, box=None, padding=(0, 2))
        table.add_column("Queue key", style="cyan")
        table.add_column("Queued", justify="right")
        table.add_column("Sched", justify="right")
        table.add_column("Running", justify="right", style="blue")
        table.add_column("Limit", justify="right")
        table.add_column("Fill", justify="right")
        table.add_column("Oldest queued", justify="right")
        for row in capacity:
            fill = row.get("fill")
            fill_str = f"{fill * 100:.0f}%" if isinstance(fill, (int, float)) else "-"
            oldest = row.get("oldest_queued_age_seconds")
            oldest_str = f"{oldest / 60:.0f}m" if isinstance(oldest, (int, float)) else "-"
            table.add_row(
                str(row.get("queue_key", "-")),
                str(row.get("queued", 0)),
                str(row.get("queued_scheduled", 0)),
                str(row.get("running", 0)),
                str(row.get("limit", 0)),
                fill_str,
                oldest_str,
            )
        console.print(table)


def _render_queue_status(status: dict[str, Any]) -> None:
    """Per-kind queued/running rollup from ``/admin/queue-status``.

    ``queue_health`` covers per-queue-key capacity (keyed by model); this adds
    the *kind* dimension (TRIAL / QA / TASK_EXPAND / ...) plus the legacy
    analysis/verdict aggregates, which are otherwise only in ``--json``.
    """
    queues = status.get("queues") or []
    by_kind: dict[str, dict[str, int]] = {}
    for row in queues:
        kind = str(row.get("kind", "TRIAL"))
        bucket = by_kind.setdefault(kind, {"queued": 0, "running": 0})
        bucket["queued"] += int(row.get("queued", 0) or 0)
        bucket["running"] += int(row.get("running", 0) or 0)

    console.print("[bold cyan]Jobs by kind[/bold cyan]")
    if by_kind:
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Kind", style="cyan")
        table.add_column("Queued", justify="right")
        table.add_column("Running", justify="right", style="blue")
        for kind in sorted(by_kind):
            bucket = by_kind[kind]
            table.add_row(kind, str(bucket["queued"]), str(bucket["running"]))
        console.print(table)
    else:
        console.print("  [dim]no active jobs[/dim]")

    # Legacy aggregate counters (kept for older deployments / drain-only kinds).
    analysis_q = int(status.get("analysis_queued", 0) or 0)
    analysis_r = int(status.get("analysis_running", 0) or 0)
    verdict_q = int(status.get("verdict_queued", 0) or 0)
    verdict_r = int(status.get("verdict_running", 0) or 0)
    if analysis_q or analysis_r or verdict_q or verdict_r:
        console.print(
            f"  [dim]analysis[/dim] queued {analysis_q} running {analysis_r}   "
            f"[dim]verdict/qa[/dim] queued {verdict_q} running {verdict_r}"
        )


def _render_slots(slots: dict[str, Any]) -> None:
    queue_keys = slots.get("queue_keys") or []
    console.print(
        f"[bold cyan]Slot leases[/bold cyan] "
        f"({slots.get('total_active', 0)}/{slots.get('total_slots', 0)} active)"
    )
    if not queue_keys:
        console.print("  [dim]no slots leased[/dim]")
        return
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Queue key", style="cyan")
    table.add_column("Active", justify="right")
    table.add_column("Total", justify="right")
    for entry in queue_keys:
        table.add_row(
            str(entry.get("queue_key", "-")),
            str(entry.get("active_slots", 0)),
            str(entry.get("total_slots", 0)),
        )
    console.print(table)


def _render_orphaned(orphaned: dict[str, Any]) -> None:
    counts = orphaned.get("counts") or {}
    stale = counts.get("running_stale_heartbeat", 0)
    stuck = counts.get("active_tasks_without_active_trials", 0)
    header_style = "red" if (stale or stuck) else "green"
    console.print(
        f"[bold cyan]Stuck / orphaned[/bold cyan] "
        f"(stale ≥ {orphaned.get('stale_after_minutes', '?')}m)"
    )
    console.print(
        f"  [{header_style}]stale-heartbeat trials {stale}[/]   "
        f"[{header_style}]tasks without active work {stuck}[/]"
    )
    samples = orphaned.get("trial_samples") or []
    if samples:
        table = Table(title="Stale trial samples", show_header=True, box=None, padding=(0, 2))
        table.add_column("Trial", style="cyan")
        table.add_column("Queue key")
        table.add_column("Stage")
        table.add_column("Worker")
        table.add_column("Slot", justify="right")
        table.add_column("Heartbeat")
        for row in samples:
            table.add_row(
                str(row.get("trial_id", "-")),
                str(row.get("queue_key", "-")),
                str(row.get("harbor_stage") or "-"),
                str(row.get("current_worker_id") or "-"),
                str(row.get("current_queue_slot") if row.get("current_queue_slot") is not None else "-"),
                _age(row.get("heartbeat_at")),
            )
        console.print(table)


def _render_worker_jobs(worker_jobs: dict[str, Any]) -> None:
    console.print("[bold cyan]Worker jobs[/bold cyan]")
    counts = worker_jobs.get("counts") or {}
    if counts:
        # Collect the union of statuses across kinds for a stable column set.
        statuses: list[str] = []
        for by_status in counts.values():
            for status_name in by_status:
                if status_name not in statuses:
                    statuses.append(status_name)
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Kind", style="cyan")
        for status_name in statuses:
            table.add_column(status_name, justify="right")
        for kind, by_status in counts.items():
            table.add_row(
                str(kind),
                *[str(by_status.get(status_name, 0)) for status_name in statuses],
            )
        console.print(table)
    else:
        console.print("  [dim]no worker jobs[/dim]")

    failures = worker_jobs.get("recent_failures") or []
    if failures:
        table = Table(title="Recent failures", show_header=True, box=None, padding=(0, 2))
        table.add_column("Job", style="cyan")
        table.add_column("Kind")
        table.add_column("Queue key")
        table.add_column("Finished")
        table.add_column("Error")
        for row in failures[:10]:
            err = str(row.get("error_message") or row.get("last_heartbeat_error") or "-")
            if len(err) > 60:
                err = err[:57] + "..."
            table.add_row(
                str(row.get("id", "-")),
                str(row.get("kind", "-")),
                str(row.get("queue_key", "-")),
                _age(row.get("finished_at")),
                err,
            )
        console.print(table)


def print_queue_diagnostics(
    api_url: str,
    *,
    stale_after: int,
    json_output: bool,
) -> None:
    """Fetch and render (or emit as JSON) the queue/worker diagnostics."""
    payload, auth_error = _fetch_admin(api_url, stale_after)

    any_success = any(
        isinstance(value, dict) and "error" not in value
        for value in payload.values()
    )
    # Endpoints that returned a real error (network / non-403 HTTP). A 404 maps
    # to None (endpoint absent, e.g. worker-jobs on the core server) and is not
    # counted as an error.
    errored = {
        key: value
        for key, value in payload.items()
        if isinstance(value, dict) and "error" in value
    }

    if json_output:
        print_json(payload)
        # Non-zero exit on auth rejection, ANY failed endpoint, or a total fetch
        # failure so scripts never treat a partial/unreachable result as a
        # healthy empty queue.
        if auth_error or errored or not any_success:
            raise typer.Exit(1)
        return

    if auth_error:
        error_console.print(
            f"[red]Queue diagnostics require a full-scope API key "
            f"(got HTTP {auth_error}).[/red]\n"
            "On hosted Oddish, create a 'full' key in the dashboard; "
            "self-hosted core servers apply no auth."
        )
        raise typer.Exit(1)

    def _ok(value: object) -> bool:
        return isinstance(value, dict) and "error" not in value

    rendered_any = False

    health = payload.get("queue_health")
    if _ok(health):
        _render_health(health)
        console.print()
        rendered_any = True

    queue_status = payload.get("queue_status")
    if _ok(queue_status):
        _render_queue_status(queue_status)
        console.print()
        rendered_any = True

    slots = payload.get("slots")
    if _ok(slots):
        _render_slots(slots)
        console.print()
        rendered_any = True

    orphaned = payload.get("orphaned_state")
    if _ok(orphaned):
        _render_orphaned(orphaned)
        console.print()
        rendered_any = True

    worker_jobs = payload.get("worker_jobs")
    if _ok(worker_jobs):
        _render_worker_jobs(worker_jobs)
        rendered_any = True

    def _summarize_errors() -> None:
        for key, value in errored.items():
            status = value.get("status")
            detail = str(value.get("error", "")).strip()
            if len(detail) > 120:
                detail = detail[:117] + "..."
            error_console.print(f"  [dim]{key}: {status or ''} {detail}[/dim]")

    # If nothing rendered, every endpoint either errored or was unavailable
    # (e.g. the API is unreachable). Don't let that masquerade as a healthy
    # empty queue -- report the failure and exit non-zero.
    if not rendered_any:
        error_console.print(
            "[red]Could not fetch any queue diagnostics.[/red] "
            "The API may be unreachable, or these endpoints are unavailable "
            "on this deployment."
        )
        _summarize_errors()
        raise typer.Exit(1)

    # Some sections rendered but others failed: surface the partial failure and
    # exit non-zero so the missing data isn't silently ignored.
    if errored:
        error_console.print(
            "[yellow]Some queue diagnostics could not be fetched:[/yellow]"
        )
        _summarize_errors()
        raise typer.Exit(1)
