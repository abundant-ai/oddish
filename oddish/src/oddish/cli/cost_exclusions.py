"""``oddish cost-exclusions`` — declare spend that isn't real.

Wraps the operator-only admin endpoints behind the same two lists the admin
dashboard edits:

- ``/admin/cost-excluded-models``       -> models that cost nothing
- ``/admin/cost-excluded-experiments``  -> experiments that were comped

Excluded spend still shows on experiment, task, and trial pages -- labelled as
not real -- but is dropped from the admin cost dashboards and from quota
enforcement. Operator-gated on hosted Oddish (a full-scope API key belonging
to the operator org); a self-hosted core server does not expose these routes.
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

cost_exclusions_app = typer.Typer(
    help="Manage the models and experiments whose spend doesn't count.",
    no_args_is_help=True,
)

# kind -> (endpoint path, request field the reference goes in, display noun).
# ``add`` and ``remove`` both key off this so a new axis is one entry.
_KINDS: dict[str, tuple[str, str, str]] = {
    "model": ("/admin/cost-excluded-models", "model_name", "model"),
    "experiment": ("/admin/cost-excluded-experiments", "experiment", "experiment"),
}


def _kind(value: str) -> tuple[str, str, str]:
    try:
        return _KINDS[value]
    except KeyError:
        error_console.print(
            f"[red]Unknown kind '{value}'.[/red] Expected one of: "
            f"{', '.join(sorted(_KINDS))}."
        )
        raise typer.Exit(2) from None


def _request(
    method: str,
    api_url: str,
    path: str,
    *,
    json_output: bool,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """One admin call, with the shared gate/absence error vocabulary.

    Mirrors ``oddish costs``: 401/403 means the key isn't operator-scoped,
    404 on the collection means the deployment has no such route at all.
    """
    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.request(
                method, f"{api_url}{path}", json=json_body, follow_redirects=True
            )
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code in (401, 403):
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            error_console.print(
                f"[red]Cost exclusions require a full-scope API key in the "
                f"operator organization (got HTTP {response.status_code}).[/red]"
            )
        raise typer.Exit(1)
    if response.status_code == 503:
        if json_output:
            print_json({"error": response.text, "status": 503})
        else:
            error_console.print(
                "[red]Cost exclusions are not available yet (the schema is "
                "still migrating). Try again shortly.[/red]"
            )
        raise typer.Exit(1)
    if response.status_code != 200:
        if json_output:
            print_json({"error": response.text, "status": response.status_code})
        else:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except ValueError:
                pass
            error_console.print(f"[red]Request failed (HTTP "
                                f"{response.status_code}):[/red] {detail}")
        raise typer.Exit(1)

    return response.json()


def _render_models(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("[dim]No models are excluded from cost.[/dim]")
        return
    table = Table(title="Cost-excluded models", show_header=True)
    table.add_column("Model", style="cyan")
    table.add_column("Label")
    table.add_column("Row id", style="dim")
    for row in rows:
        table.add_row(
            row.get("model_name", "-"), row.get("label") or "-", row.get("id", "-")
        )
    console.print(table)


def _render_experiments(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("[dim]No experiments are excluded from cost.[/dim]")
        return
    table = Table(title="Cost-excluded experiments", show_header=True)
    table.add_column("Experiment", style="cyan")
    table.add_column("Experiment id", style="dim")
    table.add_column("Label")
    table.add_column("Row id", style="dim")
    for row in rows:
        table.add_row(
            row.get("experiment_name") or row.get("experiment_id", "-"),
            row.get("experiment_id", "-"),
            row.get("label") or "-",
            row.get("id", "-"),
        )
    console.print(table)


@cost_exclusions_app.command("list")
def list_exclusions(
    kind: Annotated[
        Optional[str],
        typer.Option(
            "--kind",
            help="Limit to one axis: 'model' or 'experiment'. Default: both.",
        ),
    ] = None,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the raw exclusion lists as JSON.")
    ] = False,
) -> None:
    """Show which models and experiments don't count toward spend."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    kinds = [kind] if kind else ["model", "experiment"]
    for value in kinds:
        _kind(value)

    payload: dict[str, Any] = {}
    for value in kinds:
        path, _, _ = _KINDS[value]
        payload[f"{value}s"] = _request(
            "GET", api_url, path, json_output=json_output
        )

    if json_output:
        print_json(payload)
        return

    if "model" in kinds:
        _render_models(payload["models"])
    if "experiment" in kinds:
        _render_experiments(payload["experiments"])


@cost_exclusions_app.command("add")
def add_exclusion(
    kind: Annotated[
        str, typer.Argument(help="What to exclude: 'model' or 'experiment'.")
    ],
    reference: Annotated[
        str,
        typer.Argument(
            help="Model id (e.g. 'xai/grok-4'), or an experiment name or id."
        ),
    ],
    label: Annotated[
        str,
        typer.Option("--label", help="Why it doesn't count, e.g. 'sponsored'."),
    ] = "",
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the created row as JSON.")
    ] = False,
) -> None:
    """Stop a model's or an experiment's spend counting toward cost.

    Takes effect retroactively: spend already recorded against a newly
    excluded model or experiment leaves the cost dashboards and stops
    counting against quotas.
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    path, field, noun = _kind(kind)
    row = _request(
        "POST",
        api_url,
        path,
        json_output=json_output,
        json_body={field: reference, "label": label},
    )

    if json_output:
        print_json(row)
        return
    name = row.get("model_name") or row.get("experiment_name") or reference
    console.print(f"[green]Excluded {noun}[/green] {name} [dim]({row.get('id')})[/dim]")


@cost_exclusions_app.command("remove")
def remove_exclusion(
    kind: Annotated[
        str, typer.Argument(help="Which list to remove from: 'model' or 'experiment'.")
    ],
    reference: Annotated[
        str,
        typer.Argument(
            help="The row id from `list`, or the model / experiment id or name."
        ),
    ],
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the deletion result as JSON.")
    ] = False,
) -> None:
    """Put a model's or an experiment's spend back into cost accounting.

    Also retroactive: the exclusion only ever hid the spend, so removing it
    restores every dollar to the dashboards and to quota sums.
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    path, _, noun = _kind(kind)
    rows = _request("GET", api_url, path, json_output=json_output)

    # Accept whatever the operator has to hand -- the row id `list` printed,
    # or the model/experiment they originally excluded. Matching the row id
    # first keeps it unambiguous when a name happens to collide with an id.
    match = next((row for row in rows if row.get("id") == reference), None)
    if match is None:
        candidates = [
            row
            for row in rows
            if reference
            in (
                row.get("model_name"),
                row.get("experiment_id"),
                row.get("experiment_name"),
            )
        ]
        if len(candidates) > 1:
            error_console.print(
                f"[red]'{reference}' matches {len(candidates)} entries;[/red] "
                "use the row id from `oddish cost-exclusions list`."
            )
            raise typer.Exit(1)
        match = candidates[0] if candidates else None
    if match is None:
        error_console.print(
            f"[red]No excluded {noun} matches '{reference}'.[/red] "
            "Run `oddish cost-exclusions list` to see the current entries."
        )
        raise typer.Exit(1)

    result = _request(
        "DELETE", api_url, f"{path}/{match['id']}", json_output=json_output
    )
    if json_output:
        print_json(result)
        return
    name = match.get("model_name") or match.get("experiment_name") or reference
    console.print(f"[green]Re-included {noun}[/green] {name} [dim]({match['id']})[/dim]")
