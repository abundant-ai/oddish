from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from oddish.config import settings
from oddish.cursor_billing import CursorBillingError, fetch_all_team_spend

console = Console()


def cursor_spend(
    search: Annotated[
        str | None,
        typer.Option("--search", help="Filter members by name or email."),
    ] = None,
    sort_by: Annotated[
        str | None,
        typer.Option("--sort-by", help="Sort by: amount | date | user."),
    ] = "amount",
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit raw JSON instead of a table."),
    ] = False,
) -> None:
    """Show ACTUAL Cursor team spend for the current cycle (Cursor Admin API).

    Authoritative billing pulled from api.cursor.com/teams/spend, distinct from
    the per-trial token-ESTIMATED cursor-cli cost shown on tasks. Requires a
    Cursor team admin API key in CURSOR_ADMIN_API_KEY.
    """
    key = settings.cursor_admin_api_key
    if not key:
        console.print(
            "[red]CURSOR_ADMIN_API_KEY is not set.[/red] Create a team admin "
            "API key in Cursor (Dashboard -> Settings -> Admin API) and export it."
        )
        raise typer.Exit(1)

    try:
        spend = fetch_all_team_spend(
            key, search_term=search, sort_by=sort_by, sort_direction="desc"
        )
    except CursorBillingError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if json_out:
        console.print_json(
            data={
                "subscription_cycle_start_ms": spend.subscription_cycle_start_ms,
                "total_members": spend.total_members,
                "total_overall_spend_usd": round(spend.total_overall_spend_usd, 2),
                "total_on_demand_spend_usd": round(spend.total_on_demand_spend_usd, 2),
                "members": [
                    {
                        "email": m.email,
                        "name": m.name,
                        "role": m.role,
                        "overall_spend_usd": round(m.overall_spend_usd, 2),
                        "on_demand_spend_usd": round(m.spend_usd, 2),
                        "fast_premium_requests": m.fast_premium_requests,
                    }
                    for m in spend.members
                ],
            }
        )
        return

    table = Table(title="Cursor team spend (current cycle, actual)")
    table.add_column("Member")
    table.add_column("Role")
    table.add_column("On-demand $", justify="right")
    table.add_column("Overall $", justify="right")
    table.add_column("Fast reqs", justify="right")
    for m in spend.members:
        table.add_row(
            m.email or m.name or str(m.user_id),
            m.role,
            f"{m.spend_usd:,.2f}",
            f"{m.overall_spend_usd:,.2f}",
            f"{m.fast_premium_requests:,}",
        )
    console.print(table)
    console.print(
        f"[bold]Total actual spend this cycle:[/bold] "
        f"${spend.total_overall_spend_usd:,.2f} "
        f"(on-demand ${spend.total_on_demand_spend_usd:,.2f}) "
        f"across {spend.total_members} members"
    )
