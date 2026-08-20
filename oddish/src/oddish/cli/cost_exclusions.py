from __future__ import annotations

from typing import Annotated, Any, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.config import model_family_key
from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    print_json,
    require_api_key,
)

console = Console()
error_console = Console(stderr=True)

cost_exclusions_app = typer.Typer(
    help="Manage the models and experiments whose spend doesn't count.",
    no_args_is_help=True,
)

_KINDS: dict[str, tuple[str, str, str]] = {
    "model": ("/admin/cost-excluded-models", "model_name", "model"),
    "experiment": ("/admin/cost-excluded-experiments", "experiment", "experiment"),
}


def _fail(message: str, *, json_output: bool, code: int = 1) -> None:
    if json_output:
        print_json({"error": message})
    else:
        error_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code)


def _kind(value: str, *, json_output: bool = False) -> tuple[str, str, str]:
    try:
        return _KINDS[value]
    except KeyError:
        _fail(
            f"Unknown kind '{value}'. Expected one of: {', '.join(sorted(_KINDS))}.",
            json_output=json_output,
            code=2,
        )
        raise


def _request(
    method: str,
    api_url: str,
    path: str,
    *,
    json_output: bool,
    json_body: dict[str, Any] | None = None,
) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.request(
                method, f"{api_url}{path}", json=json_body, follow_redirects=True
            )
    except httpx.HTTPError as exc:
        _fail(f"Failed to connect to API: {exc}", json_output=json_output)
        raise

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
            error_console.print(
                f"[red]Request failed (HTTP {response.status_code}):[/red] {detail}"
            )
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
        _kind(value, json_output=json_output)

    payload: dict[str, Any] = {
        f"{value}s": _request("GET", api_url, _KINDS[value][0], json_output=json_output)
        for value in kinds
    }

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
    """Stop a model's or an experiment's spend counting toward cost."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    path, field, noun = _kind(kind, json_output=json_output)
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
    for created in row if isinstance(row, list) else [row]:
        name = created.get("model_name") or created.get("experiment_name") or reference
        console.print(
            f"[green]Excluded {noun}[/green] {name} [dim]({created.get('id')})[/dim]"
        )


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
    """Put a model's or an experiment's spend back into cost accounting."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    path, _, noun = _kind(kind, json_output=json_output)
    rows = _request("GET", api_url, path, json_output=json_output)

    matches = [row for row in rows if row.get("id") == reference]
    if not matches and kind == "model":
        key = model_family_key(reference)
        matches = [r for r in rows if model_family_key(r.get("model_name")) == key]
    elif not matches:
        matches = [
            r
            for r in rows
            if reference in {r.get("experiment_id"), r.get("experiment_name")}
        ]
        if len(matches) > 1:
            _fail(
                f"'{reference}' matches {len(matches)} entries; use the row "
                "id from `oddish cost-exclusions list`.",
                json_output=json_output,
            )
    if not matches:
        _fail(
            f"No excluded {noun} matches '{reference}'. Run "
            "`oddish cost-exclusions list` to see the current entries.",
            json_output=json_output,
        )

    results = [
        _request("DELETE", api_url, f"{path}/{row['id']}", json_output=json_output)
        for row in matches
    ]
    if json_output:
        print_json(results if len(results) > 1 else results[0])
        return
    for row in matches:
        name = row.get("model_name") or row.get("experiment_name") or reference
        console.print(
            f"[green]Re-included {noun}[/green] {name} [dim]({row['id']})[/dim]"
        )
