"""``oddish reports`` — author + browse synthetic shareable runs.

The whole point of this sub-app is that an AI agent can write a YAML
spec, hand it to ``oddish reports create``, and end up with a
shareable, persistent, optionally-backfillable curated grid of trials.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from oddish.cli.config import (
    error_console,
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)
from oddish.schemas import ReportSpec


reports_app = typer.Typer(
    help="Author and manage reports — synthetic shareable runs.",
    no_args_is_help=True,
)
console = Console()

REQUEST_TIMEOUT = 60.0


def _load_spec_file(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    if path.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(raw) or {}
    if path.suffix.lower() == ".json":
        return json.loads(raw)
    # Fall back to YAML — it accepts JSON too.
    return yaml.safe_load(raw) or {}


def _resolve_api(api_url: str | None) -> str:
    resolved = api_url or get_api_url()
    require_api_key(resolved)
    return resolved


def _request(
    method: str,
    api_url: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, headers=get_auth_headers(api_url)
        ) as client:
            return client.request(
                method, f"{api_url}{path}", json=json_body, params=params
            )
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to call API:[/red] {exc}")
        raise typer.Exit(1) from exc


def _report_url(api_url: str, report_id: str) -> str:
    return f"{get_dashboard_url(api_url)}/reports/{report_id}"


def _share_url(api_url: str, public_token: str) -> str:
    return f"{get_dashboard_url(api_url)}/share/reports/{public_token}"


def _print_report_summary(report: dict, api_url: str) -> None:
    name = report.get("name", "(unnamed)")
    rid = report.get("id", "")
    description = report.get("description") or ""
    rows = len(report.get("tasks", []))
    cols = len(report.get("columns", []))
    total_trials = report.get("total_trials", 0)
    total_missing = report.get("total_missing", 0)
    is_public = report.get("is_public", False)
    public_token = report.get("public_token")

    body_lines = [
        f"[bold]{name}[/bold] [dim]{rid}[/dim]",
        description,
        "",
        f"Grid: [cyan]{rows} task version(s) × {cols} agent(s)[/cyan]",
        f"Trials: [green]{total_trials}[/green]"
        + (
            f"  Missing: [yellow]{total_missing}[/yellow]"
            if total_missing
            else ""
        ),
        f"View:   {_report_url(api_url, rid)}",
    ]
    if is_public and public_token:
        body_lines.append(f"Public: {_share_url(api_url, public_token)}")
    console.print(Panel("\n".join(line for line in body_lines if line is not None)))


def _print_cell_grid(report: dict) -> None:
    """Render the per-task / per-column trial counts as a quick grid.

    The wire response carries one ``TaskStatusResponse`` per row (matching
    the experiment endpoint), so we group its trials by the requested
    column ``(agent, model)`` pairs to derive the per-cell counts the
    CLI used to read directly off ``cells``.
    """
    tasks = report.get("tasks") or []
    columns = report.get("columns") or []
    if not tasks or not columns:
        return

    def col_key(agent: str | None, model: str | None) -> tuple[str, str | None]:
        return (agent or "", model)

    expected = [col_key(c.get("agent"), c.get("model")) for c in columns]

    table = Table(title="Cells", show_lines=False)
    table.add_column("Task / Version", style="cyan", no_wrap=True)
    for col in columns:
        table.add_column(col["column_key"], no_wrap=True)

    for task in tasks:
        cur_v = task.get("current_version")
        row_label = f"{task.get('name', '?')} v{cur_v}" if cur_v else task.get("name", "?")
        counts: dict[tuple[str, str | None], int] = {}
        for trial in task.get("trials") or []:
            counts[col_key(trial.get("agent"), trial.get("model"))] = (
                counts.get(col_key(trial.get("agent"), trial.get("model")), 0) + 1
            )
        cells_row: list[str] = [row_label]
        for key in expected:
            n = counts.get(key, 0)
            cells_row.append(
                f"[green]{n}[/green]" if n > 0 else "[dim]0[/dim]"
            )
        table.add_row(*cells_row)
    console.print(table)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@reports_app.command("create")
def create(
    spec: Annotated[
        Path,
        typer.Option(
            "--spec",
            "-s",
            exists=True,
            readable=True,
            help="Path to YAML or JSON spec",
        ),
    ],
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the raw JSON response")
    ] = False,
) -> None:
    """Create a report from a YAML/JSON spec.

    Validates the spec locally first so the agent gets a clear error
    message before bothering the server.
    """
    api = _resolve_api(api_url)
    raw_spec = _load_spec_file(spec)
    try:
        validated = ReportSpec.model_validate(raw_spec)
    except ValidationError as exc:
        error_console.print("[red]Spec failed validation:[/red]")
        error_console.print(exc)
        raise typer.Exit(1) from exc

    payload = validated.model_dump(mode="json", exclude_none=False)
    response = _request("POST", api, "/reports", json_body=payload)
    if response.status_code >= 400:
        error_console.print(f"[red]Create failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)

    report = response.json()
    if json_output:
        print(json.dumps(report, indent=2, default=str))
        return

    console.print(
        f"[green]Created report[/green] [bold]{report['id']}[/bold]: {report['name']}"
    )
    _print_report_summary(report, api)


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


@reports_app.command("ls")
def ls(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=200)] = 50,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    api_url: Annotated[str, typer.Option("--api")] = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List reports for your organization, newest first."""
    api = _resolve_api(api_url)
    response = _request(
        "GET", api, "/reports", params={"limit": limit, "offset": offset}
    )
    if response.status_code >= 400:
        error_console.print(f"[red]List failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)

    items = response.json()
    if json_output:
        print(json.dumps(items, indent=2, default=str))
        return

    if not items:
        console.print("[dim]No reports yet — create one with `oddish reports create --spec <file>`.[/dim]")
        return

    table = Table(title="Reports", show_header=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Rows", justify="right", no_wrap=True)
    table.add_column("Cols", justify="right", no_wrap=True)
    table.add_column("Public", no_wrap=True)
    table.add_column("Updated", no_wrap=True)
    for item in items:
        public = (
            f"[green]✓[/green] {item['public_token'][:8]}…"
            if item.get("is_public") and item.get("public_token")
            else "[dim]-[/dim]"
        )
        table.add_row(
            item["id"],
            item.get("name") or "(unnamed)",
            str(item.get("rows_count", 0)),
            str(item.get("columns_count", 0)),
            public,
            str(item.get("updated_at", ""))[:16],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@reports_app.command("get")
def get(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    api_url: Annotated[str, typer.Option("--api")] = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Fetch a report and print its summary + cell grid."""
    api = _resolve_api(api_url)
    response = _request("GET", api, f"/reports/{report_id}")
    if response.status_code >= 400:
        error_console.print(f"[red]Get failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)

    report = response.json()
    if json_output:
        print(json.dumps(report, indent=2, default=str))
        return

    _print_report_summary(report, api)
    _print_cell_grid(report)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@reports_app.command("pull")
def pull(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    output: Annotated[
        str,
        typer.Option(
            "--output",
            "-o",
            help=(
                "Output path. Defaults to '<id>.yaml'. Pass '-' to write to "
                "stdout (no trailing prompt)."
            ),
        ),
    ] = "",
    api_url: Annotated[str, typer.Option("--api")] = "",
) -> None:
    """Download the canonical YAML spec for a report.

    The output round-trips into ``oddish reports create --spec <file>``
    on a different org/key so agents can clone, edit, and re-submit.
    """
    api = _resolve_api(api_url)
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, headers=get_auth_headers(api)
        ) as client:
            response = client.get(f"{api}/reports/{report_id}.yaml")
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to call API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code >= 400:
        error_console.print(f"[red]Pull failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)

    body = response.text
    if output == "-":
        sys.stdout.write(body)
        return

    dest = Path(output) if output else Path(f"{report_id}.yaml")
    dest.write_text(body)
    console.print(f"[green]Saved spec to[/green] {dest}")


# ---------------------------------------------------------------------------
# backfill / backfill-history
# ---------------------------------------------------------------------------


@reports_app.command("backfill")
def backfill(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the confirmation prompt and submit immediately.",
        ),
    ] = False,
    api_url: Annotated[str, typer.Option("--api")] = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Plan and (optionally) execute a backfill for missing cells.

    Without ``--yes`` this is a dry run that prints the plan + cost
    estimate and exits. The cost estimate uses the historical mean
    ``cost_usd`` of trials with matching ``(agent, model)`` pairs in
    your org; sample sizes <5 are flagged.
    """
    api = _resolve_api(api_url)
    plan_resp = _request("GET", api, f"/reports/{report_id}/backfill/plan")
    if plan_resp.status_code >= 400:
        error_console.print(f"[red]Plan failed ({plan_resp.status_code}):[/red] {plan_resp.text}")
        raise typer.Exit(1)
    plan = plan_resp.json()

    if json_output:
        print(json.dumps(plan, indent=2, default=str))

    total_missing = plan.get("total_missing", 0)
    if total_missing == 0:
        console.print("[green]Nothing to backfill — all cells are full.[/green]")
        return

    est = plan.get("total_est_cost_usd")
    sample = plan.get("total_cost_sample_size", 0)
    if not json_output:
        table = Table(title="Backfill plan", show_header=True)
        table.add_column("Row", justify="right")
        table.add_column("Col", justify="right")
        table.add_column("Column key")
        table.add_column("Agent / Model")
        table.add_column("Have", justify="right")
        table.add_column("Need", justify="right")
        table.add_column("Est $", justify="right")
        table.add_column("Sample n", justify="right")
        for cell in plan.get("cells", []):
            est_str = (
                f"${cell['est_cost_usd']:.2f}"
                if cell.get("est_cost_usd") is not None
                else "[dim]?[/dim]"
            )
            sample_n = cell.get("cost_sample_size", 0)
            sample_str = (
                f"[yellow]{sample_n}[/yellow]"
                if 0 < sample_n < 5
                else (str(sample_n) if sample_n else "[dim]0[/dim]")
            )
            table.add_row(
                str(cell["row_idx"]),
                str(cell["col_idx"]),
                cell["column_key"],
                f"{cell['agent']} / {cell.get('model') or '-'}",
                str(cell["have"]),
                str(cell["need"]),
                est_str,
                sample_str,
            )
        console.print(table)

        missing_cfg = plan.get("columns_missing_backfill_config", [])
        if missing_cfg:
            error_console.print(
                f"[red]Cannot run backfill — these columns need a "
                f"`backfill` block in the spec:[/red] {', '.join(missing_cfg)}"
            )
            raise typer.Exit(1)

        est_text = (
            f"~${est:.2f}" if est is not None else "unavailable"
        )
        warning = " [yellow](low sample size)[/yellow]" if 0 < sample < 5 else ""
        console.print(
            f"[bold]Will queue {total_missing} trials. Estimated cost: "
            f"{est_text}{warning}[/bold]"
        )

    if not yes:
        if not sys.stdin.isatty():
            error_console.print(
                "[yellow]Non-interactive run; re-run with --yes to submit.[/yellow]"
            )
            raise typer.Exit(2)
        if not typer.confirm("Proceed?", default=False):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    exec_resp = _request(
        "POST",
        api,
        f"/reports/{report_id}/backfill/execute",
        json_body={"cells": None, "confirm": True, "source": "cli"},
    )
    if exec_resp.status_code >= 400:
        error_console.print(
            f"[red]Execute failed ({exec_resp.status_code}):[/red] {exec_resp.text}"
        )
        raise typer.Exit(1)
    result = exec_resp.json()
    if json_output:
        print(json.dumps(result, indent=2, default=str))
        return

    status = result.get("status", "submitted")
    color = {"submitted": "green", "partial": "yellow", "failed": "red"}.get(
        status, "white"
    )
    console.print(
        f"[{color}]{status}[/{color}] event=[cyan]{result['event_id']}[/cyan] "
        f"submitted={result['submitted']} "
        f"backfill_experiment={result['backfill_experiment_id']}"
    )


@reports_app.command("backfill-history")
def backfill_history(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 50,
    api_url: Annotated[str, typer.Option("--api")] = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the backfill audit log for a report (newest first)."""
    api = _resolve_api(api_url)
    response = _request(
        "GET",
        api,
        f"/reports/{report_id}/backfill/events",
        params={"limit": limit},
    )
    if response.status_code >= 400:
        error_console.print(f"[red]History failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)

    events = response.json()
    if json_output:
        print(json.dumps(events, indent=2, default=str))
        return

    if not events:
        console.print("[dim]No backfill events yet.[/dim]")
        return

    table = Table(title="Backfill history", show_header=True)
    table.add_column("Event", style="cyan", no_wrap=True)
    table.add_column("When", no_wrap=True)
    table.add_column("By", no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Trials", justify="right")
    table.add_column("Est $", justify="right")
    table.add_column("Status")
    for ev in events:
        est = ev.get("est_cost_usd_total")
        est_str = f"${est:.2f}" if est is not None else "[dim]?[/dim]"
        table.add_row(
            ev["id"],
            str(ev.get("initiated_at") or "")[:16],
            ev.get("initiated_by_display") or "[dim]?[/dim]",
            ev.get("source", "-"),
            str(ev.get("trials_submitted", 0)),
            est_str,
            ev.get("status", "-"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# share / delete
# ---------------------------------------------------------------------------


@reports_app.command("share")
def share(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    unpublish: Annotated[
        bool,
        typer.Option(
            "--unpublish",
            help="Revoke the public link (the token is preserved but stops working).",
        ),
    ] = False,
    api_url: Annotated[str, typer.Option("--api")] = "",
) -> None:
    """Publish (default) or unpublish (--unpublish) a report's public link."""
    api = _resolve_api(api_url)
    method = "DELETE" if unpublish else "POST"
    response = _request(method, api, f"/reports/{report_id}/share")
    if response.status_code >= 400:
        error_console.print(f"[red]Share update failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)
    data = response.json()

    if data.get("is_public") and data.get("public_token"):
        console.print(
            f"[green]Public:[/green] {_share_url(api, data['public_token'])}"
        )
    else:
        console.print("[dim]Report is private.[/dim]")


@reports_app.command("delete")
def delete(
    report_id: Annotated[str, typer.Argument(help="Report ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    api_url: Annotated[str, typer.Option("--api")] = "",
) -> None:
    """Soft-delete a report. Trials and audit history are preserved."""
    api = _resolve_api(api_url)
    if not yes:
        if not sys.stdin.isatty():
            error_console.print("[yellow]Re-run with --yes to confirm.[/yellow]")
            raise typer.Exit(2)
        if not typer.confirm(f"Delete report {report_id}?", default=False):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    response = _request("DELETE", api, f"/reports/{report_id}")
    if response.status_code >= 400:
        error_console.print(f"[red]Delete failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)
    console.print(f"[green]Deleted[/green] {report_id}")
