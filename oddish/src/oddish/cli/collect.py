from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()


def _dedupe(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


def _guard_sources(*, tasks: list[str], trial_ids: list[str]) -> bool:
    return bool(_dedupe(tasks) or _dedupe(trial_ids))


def _build_payload(*, name: str, tasks: list[str], trial_ids: list[str]) -> dict:
    return {"name": name, "task_ids": _dedupe(tasks), "trial_ids": _dedupe(trial_ids)}


def _share_url(api_url: str, public_token: str | None) -> str | None:
    if not public_token:
        return None
    return f"{get_dashboard_url(api_url)}/share/{public_token}"


def collect(
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to include (optional; combine with --task)."),
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option("--task", "-t", help="Task id/name; links its current-version trials. Repeatable."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Name for the collection."),
    ] = None,
    publish: Annotated[
        bool,
        typer.Option("--publish/--no-publish", help="Publish a public read-only link (default: publish)."),
    ] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Gather the latest trials of one or more tasks into a read-only collection.

        oddish collect --task activiti-spring-boot-3-upgrade --task struts-rest-showcase-to-spring-mvc
        oddish collect --task my-task --no-publish -n "my rollup"
    """
    tasks = tasks or []
    trial_ids = trial_ids or []
    if not _guard_sources(tasks=tasks, trial_ids=trial_ids):
        console.print("[red]Provide at least one --task or trial id.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    coll_name = (name or "").strip() or "collection"
    payload = _build_payload(name=coll_name, tasks=tasks, trial_ids=trial_ids)

    public_url = None
    public_token = None
    publish_failed = False
    publish_status = None

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        try:
            resp = client.post(f"{api_url}/experiments/collections", json=payload)
        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)
        if resp.status_code != 200:
            console.print(f"[red]Collect failed:[/red] {resp.status_code} - {resp.text}")
            raise typer.Exit(1)
        data = resp.json()

        if publish and data.get("id"):
            pub = client.post(f"{api_url}/experiments/{data['id']}/publish")
            if pub.status_code == 200:
                # ExperimentShareResponse returns public_token, not a full URL.
                public_token = pub.json().get("public_token")
                public_url = _share_url(api_url, public_token)
            else:
                publish_failed = True
                publish_status = pub.status_code

    if json_output:
        console.print_json(
            data={**data, "public_token": public_token, "public_url": public_url}
        )
        if publish_failed:
            raise typer.Exit(1)
        return

    console.print(f"[green]Created collection {data.get('id')}[/green] ({data.get('name')})")
    console.print(f"  Trials linked:      {data.get('trials_linked', 0)}")
    console.print(f"  From tasks:         {data.get('trials_from_tasks', 0)}")
    skipped = data.get("tasks_skipped_empty", 0)
    if skipped:
        console.print(f"  Tasks skipped (empty): {skipped}")

    if public_url:
        console.print("[bold]This is a public, read-only link:[/bold]")
        console.print(f"  {public_url}")
    elif publish_failed:
        # Publish route requires a FULL-scope key while create only needs TASKS,
        # so a TASKS-scoped key creates the collection but cannot publish it.
        console.print(
            f"[red]Collection created but NOT published (it is private).[/red] "
            f"Publish returned HTTP {publish_status}."
        )
        if publish_status == 403:
            console.print(
                "  Publishing requires a FULL-scope API key; your key may be TASKS-scoped."
            )
        console.print(
            f"  View (private): {get_dashboard_url(api_url)}/experiments/{data.get('id')}"
        )
        raise typer.Exit(1)
    else:
        exp_id = data.get("id")
        if exp_id:
            console.print(f"  View: {get_dashboard_url(api_url)}/experiments/{exp_id}")
