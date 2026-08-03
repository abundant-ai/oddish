from __future__ import annotations

from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import (
    error_console,
    get_api_url,
    get_auth_headers,
    print_json,
)
from oddish.config import MAX_MODEL_CONCURRENCY, settings

console = Console()

admin_app = typer.Typer(
    help="Operator-only administration commands.", no_args_is_help=True
)
concurrency_app = typer.Typer(
    help="Inspect and change queue-key concurrency limits.",
    no_args_is_help=True,
)
admin_app.add_typer(concurrency_app, name="concurrency")


def _canonical_queue_key(queue_key: str) -> str:
    if not queue_key.strip():
        error_console.print("[red]Queue key must not be blank.[/red]")
        raise typer.Exit(1)
    return settings.normalize_queue_key(queue_key)


def _api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return response.text or f"HTTP {response.status_code}"


def _request_setting(
    client: httpx.Client,
    api_url: str,
    queue_key: str,
    *,
    allow_legacy_readback: bool = False,
    expected_override: int | None = None,
) -> dict[str, Any]:
    try:
        response = client.get(
            f"{api_url}/admin/concurrency",
            params={"queue_key": queue_key},
        )
    except httpx.RequestError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc
    if response.status_code in {404, 405} and allow_legacy_readback:
        return _request_legacy_readback(
            client,
            api_url,
            queue_key,
            expected_override=expected_override,
        )
    if response.status_code != 200:
        error_console.print(
            f"[red]Failed to read concurrency:[/red] {_api_error(response)}"
        )
        raise typer.Exit(1)
    data = response.json()
    if not isinstance(data, dict):
        error_console.print("[red]API returned an invalid concurrency response.[/red]")
        raise typer.Exit(1)
    return data


def _request_legacy_readback(
    client: httpx.Client,
    api_url: str,
    queue_key: str,
    *,
    expected_override: int | None,
) -> dict[str, Any]:
    """Verify an override through queue-health on pre-GET deployments."""
    try:
        response = client.get(f"{api_url}/admin/queue-health")
    except httpx.RequestError as exc:
        error_console.print(
            f"[red]Concurrency was updated, but legacy readback failed:[/red] {exc}"
        )
        raise typer.Exit(1) from exc
    if response.status_code != 200:
        error_console.print(
            "[red]Concurrency was updated, but legacy readback failed:[/red] "
            f"{_api_error(response)}"
        )
        raise typer.Exit(1)
    payload = response.json()
    capacity = payload.get("capacity", []) if isinstance(payload, dict) else []
    row = next(
        (
            item
            for item in capacity
            if isinstance(item, dict)
            and _canonical_queue_key(str(item.get("queue_key", ""))) == queue_key
        ),
        None,
    )
    if row is None:
        # queue-health includes every stored override, even when its queue is
        # idle. After a successful clear, absence therefore verifies that an
        # override-only queue was deleted; its deploy/effective values are not
        # available from a PUT-only server.
        if expected_override is None:
            return {
                "queue_key": queue_key,
                "limit": None,
                "deploy_limit": None,
                "override_limit": None,
                "controller_enabled": None,
                "advisory_limit": None,
                "effective_limit": None,
                "readback_source": "queue-health-absence",
            }
        error_console.print(
            "[red]Concurrency was updated, but queue-health did not return the "
            "queue for readback.[/red]"
        )
        raise typer.Exit(1)
    return {
        "queue_key": queue_key,
        "limit": row.get("limit"),
        "deploy_limit": row.get("deploy_limit"),
        "override_limit": row.get("override_limit"),
        "controller_enabled": None,
        "advisory_limit": None,
        "effective_limit": None,
        "readback_source": "queue-health",
    }


def _print_setting(data: dict[str, Any]) -> None:
    controller_enabled = data.get("controller_enabled")
    if controller_enabled is None:
        controller = "unavailable"
    elif controller_enabled:
        controller = "enabled"
    else:
        controller = "disabled"

    table = Table(title="Queue concurrency")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Queue key", str(data.get("queue_key", "")))
    table.add_row("Deploy", str(data.get("deploy_limit", "")))
    table.add_row(
        "DB override",
        "—" if data.get("override_limit") is None else str(data["override_limit"]),
    )
    table.add_row("Controller", controller)
    table.add_row(
        "Advisory",
        "—" if data.get("advisory_limit") is None else str(data["advisory_limit"]),
    )
    table.add_row(
        "Effective",
        ("—" if data.get("effective_limit") is None else str(data["effective_limit"])),
    )
    console.print(table)


def _emit_setting(data: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print_json(data)
    else:
        _print_setting(data)


def _client(api_url: str) -> httpx.Client:
    return httpx.Client(timeout=60.0, headers=get_auth_headers(api_url))


def _mutate_and_read_back(
    *,
    queue_key: str,
    limit: int | None,
    api_url: str,
) -> dict[str, Any]:
    canonical = _canonical_queue_key(queue_key)
    with _client(api_url) as client:
        try:
            response = client.put(
                f"{api_url}/admin/concurrency",
                json={"queue_key": canonical, "limit": limit},
            )
        except httpx.RequestError as exc:
            error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
            raise typer.Exit(1) from exc
        if response.status_code != 200:
            error_console.print(
                f"[red]Failed to update concurrency:[/red] {_api_error(response)}"
            )
            raise typer.Exit(1)
        mutation = response.json()
        if not isinstance(mutation, dict) or not isinstance(
            mutation.get("queue_key"), str
        ):
            error_console.print(
                "[red]API updated concurrency but returned an invalid response; "
                "readback was not possible.[/red]"
            )
            raise typer.Exit(1)
        readback = _request_setting(
            client,
            api_url,
            mutation["queue_key"],
            allow_legacy_readback=True,
            expected_override=limit,
        )

    if readback.get("queue_key") != mutation["queue_key"]:
        error_console.print(
            "[red]Concurrency readback returned a different queue key.[/red]"
        )
        raise typer.Exit(1)
    if readback.get("override_limit") != limit:
        error_console.print(
            "[red]Concurrency update did not match readback:[/red] "
            f"expected override {limit!r}, got {readback.get('override_limit')!r}."
        )
        raise typer.Exit(1)
    return readback


@concurrency_app.command("get")
def get_concurrency(
    queue_key: Annotated[str, typer.Argument(help="Queue key or model alias.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the raw setting as JSON.")
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
) -> None:
    """Show the deploy, database, controller, and effective queue limit."""
    resolved_api_url = (api_url or get_api_url()).rstrip("/")
    canonical = _canonical_queue_key(queue_key)
    with _client(resolved_api_url) as client:
        data = _request_setting(client, resolved_api_url, canonical)
    _emit_setting(data, json_output=json_output)


@concurrency_app.command("set")
def set_concurrency(
    queue_key: Annotated[str, typer.Argument(help="Queue key or model alias.")],
    limit: Annotated[
        int,
        typer.Argument(
            help="Database override limit.", min=0, max=MAX_MODEL_CONCURRENCY
        ),
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the verified setting as JSON.")
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
) -> None:
    """Set a database concurrency override, then verify it by reading it back."""
    resolved_api_url = (api_url or get_api_url()).rstrip("/")
    data = _mutate_and_read_back(
        queue_key=queue_key,
        limit=limit,
        api_url=resolved_api_url,
    )
    _emit_setting(data, json_output=json_output)


@concurrency_app.command("clear")
def clear_concurrency(
    queue_key: Annotated[str, typer.Argument(help="Queue key or model alias.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the verified setting as JSON.")
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
) -> None:
    """Clear a database override, then verify the deploy fallback."""
    resolved_api_url = (api_url or get_api_url()).rstrip("/")
    data = _mutate_and_read_back(
        queue_key=queue_key,
        limit=None,
        api_url=resolved_api_url,
    )
    _emit_setting(data, json_output=json_output)
