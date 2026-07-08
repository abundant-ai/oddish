"""``oddish costs`` — billable-spend accounting without direct DB access.

Wraps the admin cost endpoints:

- ``GET /admin/costs``              -> org-wide breakdown (by user, by model)
- ``GET /admin/costs/users/{id}``  -> one user's billed spend (by task, by model)

Admin-gated on hosted Oddish (a full-scope API key); a self-hosted core server
does not expose these routes.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, print_json, require_api_key

console = Console()
error_console = Console(stderr=True)


def _fmt_usd(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _window_label(window_days: int) -> str:
    return "all time" if window_days == 0 else f"last {window_days}d"


def _render_org_costs(data: dict[str, Any]) -> None:
    totals = data.get("totals") or {}
    console.print(
        f"[bold]Total spend:[/bold] {_fmt_usd(totals.get('cost_usd'))} "
        f"across {totals.get('trial_count', 0)} trials, "
        f"{totals.get('user_count', 0)} users, "
        f"{totals.get('experiment_count', 0)} experiments"
    )
    month_budget = totals.get("month_budget_usd")
    if month_budget is not None:
        console.print(
            f"[bold]This month:[/bold] {_fmt_usd(totals.get('month_cost_usd'))} "
            f"/ {_fmt_usd(month_budget)} budget"
        )

    by_user = data.get("by_user") or []
    if by_user:
        table = Table(title="By user", show_header=True)
        table.add_column("User", style="cyan")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in by_user[:15]:
            who = row.get("name") or row.get("email") or row.get("label") or row.get("key")
            table.add_row(str(who), _fmt_usd(row.get("cost_usd")), str(row.get("trial_count", 0)))
        console.print(table)

    by_model = data.get("by_model") or []
    if by_model:
        table = Table(title="By model", show_header=True)
        table.add_column("Model", style="cyan")
        table.add_column("Provider")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in by_model[:15]:
            table.add_row(
                str(row.get("model", "-")),
                str(row.get("provider", "-")),
                _fmt_usd(row.get("cost_usd")),
                str(row.get("trial_count", 0)),
            )
        console.print(table)


def _render_user_costs(data: dict[str, Any]) -> None:
    who = data.get("name") or data.get("email") or data.get("billed_user_id")
    console.print(f"[bold]User:[/bold] {who}")
    if data.get("email"):
        console.print(f"[bold]Email:[/bold] {data['email']}")
    totals = data.get("totals") or {}
    console.print(
        f"[bold]Billed spend:[/bold] {_fmt_usd(totals.get('cost_usd'))} "
        f"across {totals.get('trial_count', 0)} trials, "
        f"{totals.get('task_count', 0)} tasks"
    )

    tasks = data.get("tasks") or []
    if tasks:
        table = Table(title="By task", show_header=True)
        table.add_column("Task", style="cyan")
        table.add_column("Name")
        table.add_column("Cost", justify="right")
        table.add_column("Trials", justify="right")
        for row in tasks[:20]:
            table.add_row(
                str(row.get("task_id", "-")),
                (row.get("task_name") or "-")[:40],
                _fmt_usd(row.get("cost_usd")),
                str(row.get("trial_count", 0)),
            )
        console.print(table)


def costs(
    user: Annotated[
        Optional[str],
        typer.Option(
            "--user",
            help="Show one user's billed spend (by id) instead of the org-wide breakdown.",
        ),
    ] = None,
    window_days: Annotated[
        int,
        typer.Option(
            "--window-days",
            min=0,
            max=3650,
            help="Trailing window in days; 0 = all-time.",
        ),
    ] = 7,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the raw cost breakdown JSON."),
    ] = False,
) -> None:
    """Show billable-spend accounting (org-wide, or per-user with --user).

    Requires a full-scope API key on hosted Oddish; not available on a
    self-hosted core server.
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if user:
        path = f"/admin/costs/users/{user}"
    else:
        path = "/admin/costs"

    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.get(
                f"{api_url}{path}", params={"window_days": window_days}
            )
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code in (401, 403):
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(
                f"[red]Cost accounting requires a full-scope API key "
                f"(got HTTP {response.status_code}).[/red]"
            )
        raise typer.Exit(1)
    if response.status_code == 404 and not user:
        # Core server has no /admin/costs route.
        if json_output:
            print_json({"error": response.text, "status": 404})
        else:
            error_console.print(
                "[red]Cost accounting is only available on hosted Oddish.[/red]"
            )
        raise typer.Exit(1)
    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(f"[red]Failed to get costs:[/red] {response.text}")
        raise typer.Exit(1)

    data = response.json()
    if json_output:
        print_json(data)
        return

    console.print(f"[dim]Window: {_window_label(window_days)}[/dim]")
    if user:
        _render_user_costs(data)
    else:
        _render_org_costs(data)
